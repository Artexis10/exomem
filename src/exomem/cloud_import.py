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
    staging: Path | None,
    *,
    max_bytes: int,
    max_members: int = DEFAULT_MAX_MEMBERS,
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
    expect_files: int | None = None,
    expect_bytes: int | None = None,
    free_space: Callable[[Path], int] = free_bytes,
) -> UnpackResult:
    """Unpack a tar stream into `staging`, which must not exist yet.

    With `staging=None` nothing is written: every member is read and checked
    exactly as an unpack would, so an archive can be proven before anything
    is stopped or changed.

    Success means every member followed the previous one with no gap, the
    end-of-archive block came right after the last member, only zero padding
    followed it, the counts match any the sender declared, and the input was
    consumed to EOF. The caller still commits the staging directory only if
    its decryptor also exited cleanly: age authenticates its final chunk only
    at EOF.

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
        if staging is not None:
            # The caller commits with a rename and then deletes the
            # ciphertext, so the files must be on disk before success.
            os.sync()
        return result
    except BaseException:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise


def _blocks(size: int) -> int:
    return -(-size // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE


def _unpack_into(
    stream: BinaryIO,
    staging: Path | None,
    *,
    max_bytes: int,
    max_members: int,
    reserve_bytes: int,
    free_space: Callable[[Path], int],
) -> UnpackResult:
    kinds: dict[tuple[str, ...], bool] = {(): True}  # path -> is a directory
    directory_times: list[tuple[tuple[str, ...], float]] = []
    total = files = members = 0
    expected_offset = 0
    header = _end_tracking_header()
    try:
        archive = tarfile.open(fileobj=stream, mode="r|*", tarinfo=header)
    except Exception:  # noqa: BLE001 -- any parser failure is an unreadable archive
        raise ArchiveRefused(ARCHIVE_UNREADABLE, "format", 0) from None
    with archive:
        while True:
            try:
                member = archive.next()
            except Exception:  # noqa: BLE001 -- the message may quote archive content
                raise ArchiveRefused(ARCHIVE_UNREADABLE, "format", members + 1) from None
            if member is None:
                break
            members += 1
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
                    _ensure_directory(staging, parts, kinds, members)
                directory_times.append((parts, member.mtime))
                continue

            if not parts or parts in kinds:
                raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "duplicate", members)
            total += member.size
            if total > max_bytes:
                raise ArchiveRefused(ARCHIVE_TOO_LARGE, "bytes", members)
            with _filesystem_errors(members):
                _ensure_directory(staging, parts[:-1], kinds, members)
                if staging is not None and member.size > free_space(staging) - reserve_bytes:
                    raise ArchiveRefused(STORAGE_ALLOWANCE_EXCEEDED, "space", members)
                source = archive.extractfile(member)
                if source is None:
                    raise ArchiveRefused(ARCHIVE_UNREADABLE, "format", members)
                target = None if staging is None else staging.joinpath(*parts)
                _write_file(source, target, member.size, members)
            if target is not None:
                _set_mtime(target, member.mtime)
            kinds[parts] = False
            files += 1

        if not header.end_seen or header.end_offset != expected_offset:
            # `tarfile` reads a stream that stops between members as a clean
            # end; only an end-of-archive block right after the last member
            # proves nothing is missing.
            raise ArchiveRefused(ARCHIVE_UNREADABLE, "truncated", members)
        try:
            while chunk := archive.fileobj.read(_CHUNK):
                if chunk.strip(tarfile.NUL):
                    raise ArchiveRefused(ARCHIVE_UNREADABLE, "trailing", members)
        except ArchiveRefused:
            raise
        except Exception:  # noqa: BLE001 -- a bad trailer is an unreadable archive
            raise ArchiveRefused(ARCHIVE_UNREADABLE, "truncated", members) from None
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


def _end_tracking_header() -> type[tarfile.TarInfo]:
    """A `TarInfo` class that records where the end-of-archive block arrived."""

    class _Header(tarfile.TarInfo):
        end_seen = False
        end_offset = -1

        @classmethod
        def fromtarfile(cls, source: tarfile.TarFile) -> tarfile.TarInfo:
            # An all-zero header block raises EOFHeaderError; a stream that
            # simply stops raises EmptyHeaderError. `TarFile.next` returns
            # None for both, so this is the only place they differ.
            position = source.fileobj.tell()
            try:
                return super().fromtarfile(source)
            except tarfile.EOFHeaderError:
                cls.end_seen = True
                cls.end_offset = position
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
    staging: Path | None,
    parts: tuple[str, ...],
    kinds: dict[tuple[str, ...], bool],
    index: int,
) -> None:
    for depth in range(1, len(parts) + 1):
        prefix = parts[:depth]
        known = kinds.get(prefix)
        if known is True:
            continue
        if known is False:
            raise ArchiveRefused(ARCHIVE_MEMBER_REFUSED, "not_directory", index)
        if staging is not None:
            directory = staging.joinpath(*prefix)
            os.mkdir(directory, 0o700)
            # A Kubernetes fsGroup volume root is setgid, and mkdir inherits it.
            os.chmod(directory, 0o700)
        kinds[prefix] = True


def _write_file(source: BinaryIO, target: Path | None, size: int, index: int) -> None:
    """Copy exactly `size` bytes, or read and discard them when `target` is None."""

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
