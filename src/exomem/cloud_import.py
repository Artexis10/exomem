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

A refusal removes the staging directory, so a failed import leaves nothing.
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
import json
import os
import shutil
import sys
import tarfile
from collections.abc import Callable, Iterator
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

_NAME_MAX_BYTES = 255
_PATH_MAX_BYTES = 4096
_CHUNK = 1024 * 1024
_FILE_TYPES = frozenset({tarfile.REGTYPE, tarfile.AREGTYPE})
_LINK_TYPES = frozenset({tarfile.SYMTYPE, tarfile.LNKTYPE})
_FILE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class ArchiveRefused(Exception):
    """An import stopped. `reason` is a fixed token; nothing here is content."""

    def __init__(self, code: str, reason: str, member_index: int | None = None) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason
        self.member_index = member_index

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


def free_bytes(path: Path) -> int:
    """Bytes an unprivileged writer can still use on `path`'s filesystem."""

    status = os.statvfs(path)
    return status.f_bavail * status.f_frsize


def unpack_stream(
    stream: BinaryIO,
    staging: Path,
    *,
    max_bytes: int,
    max_members: int = DEFAULT_MAX_MEMBERS,
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
    expect_files: int | None = None,
    expect_bytes: int | None = None,
    free_space: Callable[[Path], int] = free_bytes,
) -> UnpackResult:
    """Unpack a tar stream into `staging`, which must not exist yet.

    Success means the archive's end-of-archive block was read, the counts
    match any the sender declared, and the input was consumed to EOF. The
    caller still commits the staging directory only if its decryptor also
    exited cleanly: age authenticates its final chunk only at EOF.

    On any refusal or error the staging directory is removed and the
    exception propagates; a directory this call did not create is never
    touched.
    """

    staging = Path(staging)
    try:
        os.mkdir(staging, 0o700)
    except OSError:
        raise ArchiveRefused(IMPORT_STAGING_UNAVAILABLE, "staging") from None
    try:
        result = _unpack_into(
            stream,
            staging,
            max_bytes=max_bytes,
            max_members=max_members,
            reserve_bytes=reserve_bytes,
            free_space=free_space,
        )
        if expect_files is not None and result.files != expect_files:
            raise ArchiveRefused(ARCHIVE_UNREADABLE, "count", result.members)
        if expect_bytes is not None and result.bytes != expect_bytes:
            raise ArchiveRefused(ARCHIVE_UNREADABLE, "count", result.members)
        # The caller commits with a rename and then deletes the ciphertext, so
        # the files must be on disk before success is reported.
        os.sync()
        return result
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _unpack_into(
    stream: BinaryIO,
    staging: Path,
    *,
    max_bytes: int,
    max_members: int,
    reserve_bytes: int,
    free_space: Callable[[Path], int],
) -> UnpackResult:
    kinds: dict[tuple[str, ...], bool] = {(): True}  # path -> is a directory
    directory_times: list[tuple[tuple[str, ...], float]] = []
    total = files = directories = members = 0
    header = _end_tracking_header()
    try:
        archive = tarfile.open(fileobj=stream, mode="r|*", tarinfo=header)
    except (tarfile.TarError, EOFError, OSError):
        raise ArchiveRefused(ARCHIVE_UNREADABLE, "format", 0) from None
    with archive:
        while True:
            try:
                member = archive.next()
            except (tarfile.TarError, EOFError, OSError, UnicodeError):
                raise ArchiveRefused(ARCHIVE_UNREADABLE, "format", members + 1) from None
            if member is None:
                break
            members += 1
            if members > max_members:
                raise ArchiveRefused(ARCHIVE_TOO_LARGE, "members", members)
            if member.type in _LINK_TYPES:
                raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "link", members)
            if member.type != tarfile.DIRTYPE and member.type not in _FILE_TYPES:
                raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "special", members)
            parts = _relative_parts(member.name, members)

            if member.type == tarfile.DIRTYPE:
                if not parts:
                    continue  # the archive's own root, as in `tar -C vault -c .`
                if kinds.get(parts) is False:
                    raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "duplicate", members)
                with _filesystem_errors(members):
                    _ensure_directory(staging, parts, kinds, members)
                directory_times.append((parts, member.mtime))
                continue

            if not parts or parts in kinds:
                raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "duplicate", members)
            if member.size < 0:
                raise ArchiveRefused(ARCHIVE_UNREADABLE, "format", members)
            total += member.size
            if total > max_bytes:
                raise ArchiveRefused(ARCHIVE_TOO_LARGE, "bytes", members)
            with _filesystem_errors(members):
                _ensure_directory(staging, parts[:-1], kinds, members)
                if member.size > free_space(staging) - reserve_bytes:
                    raise ArchiveRefused(STORAGE_ALLOWANCE_EXCEEDED, "space", members)
                source = archive.extractfile(member)
                if source is None:
                    raise ArchiveRefused(ARCHIVE_UNREADABLE, "format", members)
                target = staging.joinpath(*parts)
                _write_file(source, target, member.size, members)
            _set_mtime(target, member.mtime)
            kinds[parts] = False
            files += 1

    if not header.end_seen:
        # `tarfile` reads a stream that stops between members as a clean
        # end; only the all-zero end-of-archive block proves nothing is
        # missing.
        raise ArchiveRefused(ARCHIVE_UNREADABLE, "truncated", members)
    try:
        while stream.read(_CHUNK):
            pass  # padding after the end block; the decryptor must reach EOF
    except OSError:
        raise ArchiveRefused(ARCHIVE_UNREADABLE, "truncated", members) from None
    for parts, mtime in sorted(directory_times, key=lambda item: len(item[0]), reverse=True):
        _set_mtime(staging.joinpath(*parts), mtime)
    directories = sum(1 for path, is_dir in kinds.items() if is_dir and path)
    return UnpackResult(members=members, files=files, directories=directories, bytes=total)


def _end_tracking_header() -> type[tarfile.TarInfo]:
    """A `TarInfo` class that records whether the end-of-archive block arrived."""

    class _Header(tarfile.TarInfo):
        end_seen = False

        @classmethod
        def fromtarfile(cls, source: tarfile.TarFile) -> tarfile.TarInfo:
            # An all-zero header block raises EOFHeaderError; a stream that
            # simply stops raises EmptyHeaderError. `TarFile.next` returns
            # None for both, so this is the only place they differ.
            try:
                return super().fromtarfile(source)
            except tarfile.EOFHeaderError:
                cls.end_seen = True
                raise

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
        if error.errno in (errno.ENAMETOOLONG, errno.EINVAL, errno.EILSEQ):
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


def _ensure_directory(
    staging: Path, parts: tuple[str, ...], kinds: dict[tuple[str, ...], bool], index: int
) -> None:
    for depth in range(1, len(parts) + 1):
        prefix = parts[:depth]
        known = kinds.get(prefix)
        if known is True:
            continue
        if known is False:
            raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "not_directory", index)
        os.mkdir(staging.joinpath(*prefix), 0o700)
        kinds[prefix] = True


def _write_file(source: BinaryIO, target: Path, size: int, index: int) -> None:
    descriptor = os.open(target, _FILE_FLAGS, 0o600)
    with os.fdopen(descriptor, "wb") as sink:
        remaining = size
        while remaining:
            chunk = source.read(min(_CHUNK, remaining))
            if not chunk:
                raise ArchiveRefused(ARCHIVE_UNREADABLE, "truncated", index)
            sink.write(chunk)
            remaining -= len(chunk)


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
    unpack.add_argument("--max-bytes", required=True, type=int)
    unpack.add_argument("--max-members", type=int, default=DEFAULT_MAX_MEMBERS)
    unpack.add_argument("--reserve-bytes", type=int, default=DEFAULT_RESERVE_BYTES)
    unpack.add_argument("--expect-files", type=int, default=None)
    unpack.add_argument("--expect-bytes", type=int, default=None)
    args = parser.parse_args(argv)

    if not args.staging.is_absolute():
        print(json.dumps(ArchiveRefused(IMPORT_STAGING_UNAVAILABLE, "staging").as_dict()))
        return 1
    try:
        result = unpack_stream(
            sys.stdin.buffer,
            args.staging,
            max_bytes=args.max_bytes,
            max_members=args.max_members,
            reserve_bytes=args.reserve_bytes,
            expect_files=args.expect_files,
            expect_bytes=args.expect_bytes,
        )
    except ArchiveRefused as refused:
        print(json.dumps(refused.as_dict()))
        return 1
    print(json.dumps({"ok": True, **asdict(result)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
