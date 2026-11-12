"""Validating streaming unpacker for Exomem Cloud imports (design D6).

An import archive is the most sensitive object Cloud handles: someone's whole
set of notes. This module turns a tar stream into a directory without ever
trusting the archive's own idea of where its members go.

It reads the stream member by member and writes into a staging directory it
creates itself, never into a vault. Each member is checked before a byte of it
is written. It refuses the whole import on:

- links, devices, FIFOs and every other special entry;
- absolute paths and `..` components;
- names a cell cannot hold (control characters, invalid UTF-8, over-long
  components or paths);
- a second member at a path already written, or a member beneath a file;
- more members or bytes than the caller allows, or more bytes than the volume
  can take while keeping a reserve.

It also refuses an archive it cannot prove complete: every member must
start where the previous one ended, the end-of-archive block must follow the
last member directly, and only zero padding may come after it. Python's
streaming `tarfile` otherwise reads a stream cut between members, or a
member it could not parse, as a clean and shorter archive.

A refusal removes the staging directory, so a failed import leaves nothing.
The `verify` mode runs every check and writes nothing, so an archive can be
proven before a cell is stopped.
Moving the finished staging directory into place is the caller's single
`rename` (D6), which is atomic because staging sits on the same filesystem.

Errors and output are content-free: a code, a fixed reason token and the
member's ordinal, never a name or a path.

Standard library only, on purpose: the operator restore runbook copies this
file into a helper pod on a cell image that predates it and runs it with
`python3 cloud_import.py unpack`.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import shutil
import stat
import sys
import tarfile
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO

ARCHIVE_MEMBER_REFUSED = "ARCHIVE_MEMBER_REFUSED"
ARCHIVE_TOO_LARGE = "ARCHIVE_TOO_LARGE"
ARCHIVE_UNREADABLE = "ARCHIVE_UNREADABLE"
STORAGE_ALLOWANCE_EXCEEDED = "STORAGE_ALLOWANCE_EXCEEDED"
IMPORT_STAGING_UNAVAILABLE = "IMPORT_STAGING_UNAVAILABLE"

DEFAULT_MAX_MEMBERS = 500_000
DEFAULT_RESERVE_BYTES = 256 * 1024 * 1024
RESERVE_BYTES_ENV = "EXOMEM_IMPORT_RESERVE_BYTES"
#: What may follow the end-of-archive block when a caller stops reading there: tar pads to a whole record.
END_WINDOW_BYTES = 2 * tarfile.RECORDSIZE

_FSYNC_WORKERS = 16
_NAME_MAX_BYTES = 255
_PATH_MAX_BYTES = 4096
_CHUNK = 1024 * 1024
_FILE_TYPES = frozenset({tarfile.REGTYPE, tarfile.AREGTYPE})
#: Errors that say the member's own name cannot be stored here, so the same archive fails again. Anything else may pass.
NAME_ERRNOS = frozenset({errno.ENAMETOOLONG, errno.EINVAL, errno.EILSEQ, errno.EEXIST, errno.ENOTDIR, errno.EISDIR})
_LINK_TYPES = frozenset({tarfile.SYMTYPE, tarfile.LNKTYPE})
_FILE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


# A refusal that carries this retry value waits on the person or an operator, not on cellctl: a retry cannot fix it.
# Any refusal without a retry value may succeed on a retry. cellctl reads the value and never the code.
RETRY_OWNER = "owner"


class ArchiveRefused(Exception):
    """An import stopped. `reason` is a fixed token; nothing here is content."""

    def __init__(self, code: str, reason: str, member_index: int | None = None, retry: str | None = None) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.member_index = member_index
        self.retry = retry
        # The name of the member that was refused. Only a local caller reads it; `as_dict` never prints it.
        self.member: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": False,
            "error": self.code,
            "reason": self.reason,
            "member_index": self.member_index,
        }


@dataclass(frozen=True, slots=True)
class UnpackResult:
    members: int
    files: int
    directories: int
    bytes: int


def reserve_bytes_from_env() -> int:
    """The free space an import keeps back: the operator's setting, else the default."""
    raw = os.environ.get(RESERVE_BYTES_ENV, "").strip()
    return int(raw) if raw.isascii() and raw.isdigit() else DEFAULT_RESERVE_BYTES


@dataclass(frozen=True, slots=True)
class ReadPolicy:
    """How strictly `unpack_stream` reads. The defaults are the Cloud job's: any tar compression, read to EOF."""

    #: `tarfile`'s stream mode. `r|` reads a plain tar, for a caller that decompresses only what it allows.
    mode: str = "r|*"
    #: Stop `END_WINDOW_BYTES` past the end block, so padding after it is never read. Cloud reads to EOF for its decryptor.
    stop_after_end: bool = False
    #: Refuse two names that differ only in letter case: a vault may sit on a case-insensitive volume.
    portable_paths: bool = False
    #: The most bytes `tarfile` may read to parse one header and the pax, long-name and sparse headers chained to it.
    max_header_bytes: int | None = None


def free_bytes(path: Path) -> int:
    """Bytes an unprivileged writer can still use on `path`'s filesystem."""

    return shutil.disk_usage(path).free


def _fsync_file(path: str) -> None:
    # A walked tree can hold entries this call did not write. Only regular files are flushed, opened
    # read-only on POSIX, never through a link, and without blocking on a swapped-in pipe.
    # Windows fsync requires write access. A gone or unreadable entry is skipped.
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return
        access = os.O_RDWR if os.name == "nt" else os.O_RDONLY
        descriptor = os.open(path, access | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except (FileNotFoundError, PermissionError):
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def flush_to_disk(root: Path) -> None:
    """Put every file under `root` on disk.

    Each file is fsynced and, where a directory can be opened, each directory too, so that a request
    flushes what it wrote and nothing else. Never `os.sync`: it waits on every filesystem the host
    mounts, so one unrelated wedged mount would freeze the caller forever.
    """

    files: list[str] = []
    directories: list[str] = []
    for directory, _, names in os.walk(root):
        directories.append(directory)
        files.extend(os.path.join(directory, name) for name in names)
    # One fsync after another costs a journal commit each (about 5 ms per file measured on a local volume,
    # so 65,536 entries would pass the request budget); concurrent ones share a commit.
    with ThreadPoolExecutor(max_workers=_FSYNC_WORKERS) as pool:
        list(pool.map(_fsync_file, files))
        if hasattr(os, "O_DIRECTORY"):  # Windows cannot open a directory; the name is the portable test for POSIX
            list(pool.map(_fsync_directory, directories))


def unpack_stream(
    stream: BinaryIO,
    staging: Path | None,
    *,
    max_bytes: int,
    max_members: int = DEFAULT_MAX_MEMBERS,
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
    expect_files: int | None = None,
    expect_bytes: int | None = None,
    free_space: Callable[[Path], int] = free_bytes,
    on_file: Callable[[str, int, str], None] | None = None,
    policy: ReadPolicy | None = None,
) -> UnpackResult:
    """Unpack a tar stream into `staging`, which must not exist yet.

    With `staging=None` nothing is written: every member is read and checked
    exactly as an unpack would, so an archive can be proven before anything
    is stopped or changed.

    `on_file` is called with each file's staging-relative path, size and
    SHA-256, hashed as the file's bytes are read, so a caller can list the
    staged files without a second pass.

    Success means every member followed the previous one with no gap, the
    end-of-archive block came right after the last member, only zero padding
    followed it, the counts match any the sender declared, and the input was
    consumed to EOF. The caller still commits the staging directory only if
    its decryptor also exited cleanly: age authenticates its final chunk only
    at EOF. A `policy` that stops after the end block replaces "consumed to
    EOF" with "nothing but zeros in the window after the end block".

    On any refusal or error the staging directory is removed and the
    exception propagates; a directory this call did not create is never
    touched.
    """

    if staging is not None:
        staging = Path(staging)
        try:
            os.mkdir(staging, 0o700)
            os.chmod(staging, 0o700)
        except OSError:
            raise ArchiveRefused(IMPORT_STAGING_UNAVAILABLE, "staging") from None
    policy = policy or ReadPolicy()
    header = _end_tracking_header(policy.max_header_bytes)
    try:
        result = _unpack_into(
            stream,
            staging,
            header,
            max_bytes=max_bytes,
            max_members=max_members,
            reserve_bytes=reserve_bytes,
            free_space=free_space,
            on_file=on_file,
            policy=policy,
        )
        if expect_files is not None and result.files != expect_files:
            raise ArchiveRefused(ARCHIVE_UNREADABLE, "count", result.members)
        if expect_bytes is not None and result.bytes != expect_bytes:
            raise ArchiveRefused(ARCHIVE_UNREADABLE, "count", result.members)
        if staging is not None:
            # The caller commits with a rename and then deletes the
            # ciphertext, so the files must be on disk before success.
            flush_to_disk(staging)
        return result
    except BaseException as error:
        if isinstance(error, ArchiveRefused):
            error.member = header.member
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _blocks(size: int) -> int:
    return -(-size // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE


def _unpack_into(
    stream: BinaryIO,
    staging: Path | None,
    header: type[tarfile.TarInfo],
    *,
    max_bytes: int,
    max_members: int,
    reserve_bytes: int,
    free_space: Callable[[Path], int],
    on_file: Callable[[str, int, str], None] | None,
    policy: ReadPolicy,
) -> UnpackResult:
    kinds: dict[tuple[str, ...], bool] = {(): True}  # path -> is a directory
    # folded path -> the spelling that claimed it; None when the policy lets two spellings of a name coexist
    spellings: dict[tuple[str, ...], tuple[str, ...]] | None = {} if policy.portable_paths else None
    directory_times: list[tuple[tuple[str, ...], float]] = []
    total = files = members = 0
    expected_offset = 0
    try:
        # In stream mode `open` reads the first member's headers, so a refusal can arise here too.
        archive = tarfile.open(fileobj=stream, mode=policy.mode, tarinfo=header)
    except ArchiveRefused:
        raise
    except Exception:  # noqa: BLE001 -- any parser failure is an unreadable archive
        raise ArchiveRefused(ARCHIVE_UNREADABLE, "format", 0) from None
    with archive:
        while True:
            header.member = None
            try:
                member = archive.next()
            except ArchiveRefused:
                raise
            except Exception:  # noqa: BLE001 -- the message may quote archive content
                raise ArchiveRefused(ARCHIVE_UNREADABLE, "format", members + 1) from None
            if member is None:
                break
            members += 1
            header.member = member.name
            if members > max_members:
                raise ArchiveRefused(ARCHIVE_TOO_LARGE, "members", members)
            # A member that does not start where the previous one ended means
            # tarfile skipped something it could not parse.
            if member.offset != expected_offset or member.size < 0:
                raise ArchiveRefused(ARCHIVE_UNREADABLE, "format", members)
            expected_offset = member.offset_data + _blocks(member.size)
            if member.type in _LINK_TYPES:
                raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "link", members)
            if member.type != tarfile.DIRTYPE and member.type not in _FILE_TYPES:
                raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "special", members)
            if member.sparse is not None or any(
                key.startswith("GNU.sparse.") for key in member.pax_headers
            ):
                raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "special", members)
            parts = _relative_parts(member.name, members)

            if member.type == tarfile.DIRTYPE:
                if not parts:
                    continue  # the archive's own root, as in `tar -C vault -c .`
                if kinds.get(parts) is False:
                    raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "duplicate", members)
                with _filesystem_errors(members):
                    _ensure_directory(staging, parts, kinds, members, spellings)
                directory_times.append((parts, member.mtime))
                continue

            if not parts or parts in kinds:
                raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "duplicate", members)
            total += member.size
            if total > max_bytes:
                raise ArchiveRefused(ARCHIVE_TOO_LARGE, "bytes", members)
            with _filesystem_errors(members):
                _ensure_directory(staging, parts[:-1], kinds, members, spellings)
                _claim_spelling(spellings, parts, members)
                if staging is not None and member.size > free_space(staging) - reserve_bytes:
                    raise ArchiveRefused(STORAGE_ALLOWANCE_EXCEEDED, "space", members)
                source = archive.extractfile(member)
                if source is None:
                    raise ArchiveRefused(ARCHIVE_UNREADABLE, "format", members)
                target = None if staging is None else staging.joinpath(*parts)
                digest = hashlib.sha256() if on_file is not None else None
                _write_file(source, target, member.size, members, None if digest is None else digest.update)
            if target is not None:
                _set_mtime(target, member.mtime)
            kinds[parts] = False
            files += 1
            if on_file is not None:
                on_file("/".join(parts), member.size, digest.hexdigest())

        if not header.end_seen or header.end_offset != expected_offset:
            # `tarfile` reads a stream that stops between members as a clean
            # end; only an end-of-archive block right after the last member
            # proves nothing is missing.
            raise ArchiveRefused(ARCHIVE_UNREADABLE, "truncated", members)
        try:
            _read_trailer(archive.fileobj, header.end_offset if policy.stop_after_end else None, members)
        except ArchiveRefused:
            raise
        except Exception:  # noqa: BLE001 -- a bad trailer is an unreadable archive
            raise ArchiveRefused(ARCHIVE_UNREADABLE, "truncated", members) from None
    if not policy.stop_after_end:
        try:
            while stream.read(_CHUNK):
                pass  # a compressed stream's trailer; the decryptor must reach EOF
        except OSError:
            raise ArchiveRefused(ARCHIVE_UNREADABLE, "truncated", members) from None
    if staging is not None:
        for parts, mtime in sorted(directory_times, key=lambda item: len(item[0]), reverse=True):
            _set_mtime(staging.joinpath(*parts), mtime)
    directories = sum(1 for path, is_dir in kinds.items() if is_dir and path)
    return UnpackResult(members=members, files=files, directories=directories, bytes=total)


def _read_trailer(stream: BinaryIO, end_offset: int | None, members: int) -> None:
    """Require zeros after the end block: up to EOF, or with `end_offset` only up to `END_WINDOW_BYTES` past it."""

    while True:
        size = _CHUNK if end_offset is None else min(_CHUNK, end_offset + END_WINDOW_BYTES - stream.tell())
        if size <= 0 or not (chunk := stream.read(size)):
            return
        if chunk.strip(tarfile.NUL):
            raise ArchiveRefused(ARCHIVE_UNREADABLE, "trailing", members)


class _HeaderReads:
    """The stream as one header and its chained headers see it: all their reads together may ask for `limit` bytes."""

    def __init__(self, stream: BinaryIO, limit: int) -> None:
        self._stream = stream
        self._left = limit

    def read(self, size: int) -> bytes:
        # `tarfile` reads a pax or long-name header whole and a sparse map block by block, as the archive declares.
        self._left -= max(size, 0)
        if self._left < 0:
            raise ArchiveRefused(ARCHIVE_TOO_LARGE, "header")
        return self._stream.read(size)

    def __getattr__(self, name: str) -> object:
        return getattr(self._stream, name)


def _end_tracking_header(max_header_bytes: int | None = None) -> type[tarfile.TarInfo]:
    """A `TarInfo` class that records where the end-of-archive block arrived, and bounds a header's size."""

    class _Header(tarfile.TarInfo):
        end_seen = False
        end_offset = -1
        member: str | None = None  # the name of the member being checked; None between members

        @classmethod
        def fromtarfile(cls, source: tarfile.TarFile) -> tarfile.TarInfo:
            # An all-zero header block raises EOFHeaderError; a stream that
            # simply stops raises EmptyHeaderError. `TarFile.next` returns
            # None for both, so this is the only place they differ.
            stream = source.fileobj
            position = stream.tell()
            # `tarfile` parses the headers chained to this one by calling back into itself, through this method
            # before Python 3.14 and past it from then on, so the first call alone owns the count.
            counted = False
            if max_header_bytes is not None and not isinstance(stream, _HeaderReads):
                source.fileobj = _HeaderReads(stream, max_header_bytes)
                counted = True
            try:
                return super().fromtarfile(source)
            except tarfile.EOFHeaderError:
                cls.end_seen = True
                cls.end_offset = position
                raise
            finally:
                if counted:
                    source.fileobj = stream

    return _Header


@contextmanager
def _filesystem_errors(index: int) -> Iterator[None]:
    """Name the class of a filesystem failure without naming the member."""

    try:
        yield
    except (tarfile.TarError, EOFError):
        raise ArchiveRefused(ARCHIVE_UNREADABLE, "truncated", index) from None
    except OSError as error:
        if error.errno in (errno.ENOSPC, errno.EDQUOT):
            raise ArchiveRefused(STORAGE_ALLOWANCE_EXCEEDED, "space", index) from None
        if error.errno in NAME_ERRNOS:
            raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "name", index) from None
        raise ArchiveRefused(IMPORT_STAGING_UNAVAILABLE, "write", index) from None


def _relative_parts(name: str, index: int) -> tuple[str, ...]:
    """The member's path as validated components; `()` is the archive root."""

    try:
        encoded = name.encode("utf-8")
    except UnicodeError:
        raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "name", index) from None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in name):
        raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "name", index)
    if name.startswith("/"):
        raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "absolute", index)
    if len(encoded) > _PATH_MAX_BYTES:
        raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "name", index)
    parts: list[str] = []
    for component in name.split("/"):
        if component in ("", "."):
            continue
        if component == "..":
            raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "parent", index)
        if len(component.encode("utf-8")) > _NAME_MAX_BYTES:
            raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "name", index)
        parts.append(component)
    return tuple(parts)


def _claim_spelling(
    spellings: dict[tuple[str, ...], tuple[str, ...]] | None, parts: tuple[str, ...], index: int,
) -> None:
    """Refuse a path that differs only in letter case from one already claimed."""

    if spellings is None:
        return
    folded = tuple(part.casefold() for part in parts)
    if spellings.setdefault(folded, parts) != parts:
        raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "duplicate", index)


def _ensure_directory(
    staging: Path | None,
    parts: tuple[str, ...],
    kinds: dict[tuple[str, ...], bool],
    index: int,
    spellings: dict[tuple[str, ...], tuple[str, ...]] | None = None,
) -> None:
    for depth in range(1, len(parts) + 1):
        prefix = parts[:depth]
        known = kinds.get(prefix)
        if known is True:
            continue
        if known is False:
            raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "not_directory", index)
        _claim_spelling(spellings, prefix, index)
        if staging is not None:
            directory = staging.joinpath(*prefix)
            os.mkdir(directory, 0o700)
            # A Kubernetes fsGroup volume root is setgid, and mkdir inherits it.
            os.chmod(directory, 0o700)
        kinds[prefix] = True


def _write_file(
    source: BinaryIO, target: Path | None, size: int, index: int,
    hash_update: Callable[[bytes], object] | None = None,
) -> None:
    """Copy exactly `size` bytes, or read and discard them when `target` is None; `hash_update` sees each chunk."""

    sink: BinaryIO | None = None
    if target is not None:
        sink = os.fdopen(os.open(target, _FILE_FLAGS, 0o600), "wb")
    try:
        remaining = size
        while remaining:
            try:
                chunk = source.read(min(_CHUNK, remaining))
            except Exception:  # noqa: BLE001 -- the message may quote archive content
                raise ArchiveRefused(ARCHIVE_UNREADABLE, "truncated", index) from None
            if not chunk:
                raise ArchiveRefused(ARCHIVE_UNREADABLE, "truncated", index)
            if sink is not None:
                sink.write(chunk)
            if hash_update is not None:
                hash_update(chunk)
            remaining -= len(chunk)
    finally:
        if sink is not None:
            sink.close()


def _set_mtime(path: Path, mtime: float) -> None:
    try:
        os.utime(path, (mtime, mtime), follow_symlinks=False)
    except (OSError, OverflowError, ValueError):
        pass  # a timestamp is metadata; never a reason to fail an import


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cloud_import",
        description="Validate and unpack an Exomem Cloud import stream from stdin.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    unpack = commands.add_parser("unpack", help="unpack a tar stream into a new staging dir")
    unpack.add_argument("--staging", required=True, type=Path)
    unpack.add_argument("--reserve-bytes", type=int, default=DEFAULT_RESERVE_BYTES)
    verify = commands.add_parser("verify", help="check a tar stream without writing anything")
    for command in (unpack, verify):
        command.add_argument("--max-bytes", required=True, type=int)
        command.add_argument("--max-members", type=int, default=DEFAULT_MAX_MEMBERS)
        command.add_argument("--expect-files", type=int, default=None)
        command.add_argument("--expect-bytes", type=int, default=None)
    args = parser.parse_args(argv)

    staging = args.staging if args.command == "unpack" else None
    if staging is not None and not staging.is_absolute():
        print(json.dumps(ArchiveRefused(IMPORT_STAGING_UNAVAILABLE, "staging").as_dict()))
        return 1
    try:
        result = unpack_stream(
            sys.stdin.buffer,
            staging,
            max_bytes=args.max_bytes,
            max_members=args.max_members,
            reserve_bytes=getattr(args, "reserve_bytes", 0),
            expect_files=args.expect_files,
            expect_bytes=args.expect_bytes,
        )
    except ArchiveRefused as refused:
        print(json.dumps(refused.as_dict()))
        return 1
    except Exception:  # noqa: BLE001 -- never print a traceback that may quote content
        print(json.dumps(ArchiveRefused(ARCHIVE_UNREADABLE, "internal").as_dict()))
        return 1
    print(json.dumps({"ok": True, **asdict(result)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
