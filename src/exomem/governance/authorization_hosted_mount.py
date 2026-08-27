"""Copy one Kubernetes-projected authorization bundle into owner-only memory."""

from __future__ import annotations

import os
import re
import stat
import sys
import uuid
from pathlib import Path

from .authorization_custody import HOSTED_CUSTODY_ROOT, MAX_CUSTODY_FILE_BYTES

SOURCE_ROOT = Path("/run/exomem/authorization-session-source")
_FILENAMES = ("control.json", "keyring.json", "serving-membership.json")
_GENERATION = re.compile(r"\.\.[A-Za-z0-9_.-]{1,255}\Z")


class HostedCustodyMountUnavailable(RuntimeError):
    """Content-free refusal for an unsafe or incomplete projected bundle."""


def _read_exact(descriptor: int, expected_size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = MAX_CUSTODY_FILE_BYTES + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 4096))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    result = b"".join(chunks)
    if len(result) != expected_size or len(result) > MAX_CUSTODY_FILE_BYTES:
        raise HostedCustodyMountUnavailable
    return result


def _projected_payloads(source: Path) -> dict[str, bytes]:
    try:
        root = os.lstat(source)
        if not stat.S_ISDIR(root.st_mode) or stat.S_ISLNK(root.st_mode):
            raise HostedCustodyMountUnavailable
        generation = os.readlink(source / "..data")
        if _GENERATION.fullmatch(generation) is None:
            raise HostedCustodyMountUnavailable
        expected_entries = {"..data", generation, *_FILENAMES}
        if set(os.listdir(source)) != expected_entries:
            raise HostedCustodyMountUnavailable
        generation_info = os.lstat(source / generation)
        if not stat.S_ISDIR(generation_info.st_mode) or stat.S_ISLNK(
            generation_info.st_mode
        ):
            raise HostedCustodyMountUnavailable
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        generation_fd = os.open(source / generation, flags)
        try:
            payloads: dict[str, bytes] = {}
            for name in _FILENAMES:
                if os.readlink(source / name) != f"..data/{name}":
                    raise HostedCustodyMountUnavailable
                file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
                    os, "O_NOFOLLOW", 0
                )
                descriptor = os.open(name, file_flags, dir_fd=generation_fd)
                try:
                    before = os.fstat(descriptor)
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_nlink != 1
                        or not 1 <= before.st_size <= MAX_CUSTODY_FILE_BYTES
                        or stat.S_IMODE(before.st_mode) not in {0o400, 0o440, 0o444}
                    ):
                        raise HostedCustodyMountUnavailable
                    payloads[name] = _read_exact(descriptor, before.st_size)
                    after = os.fstat(descriptor)
                    if (
                        before.st_dev,
                        before.st_ino,
                        before.st_size,
                        before.st_mtime_ns,
                        before.st_ctime_ns,
                    ) != (
                        after.st_dev,
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ):
                        raise HostedCustodyMountUnavailable
                finally:
                    os.close(descriptor)
            if os.readlink(source / "..data") != generation or any(
                os.readlink(source / name) != f"..data/{name}"
                for name in _FILENAMES
            ):
                raise HostedCustodyMountUnavailable
            return payloads
        finally:
            os.close(generation_fd)
    except HostedCustodyMountUnavailable:
        raise
    except (OSError, TypeError, ValueError):
        raise HostedCustodyMountUnavailable from None


def copy_projected_custody(source: Path, destination: Path) -> None:
    """Publish exactly three stable 0600 files from one projected generation."""

    source = Path(source)
    destination = Path(destination)
    staged: list[Path] = []
    try:
        payloads = _projected_payloads(source)
        info = os.lstat(destination)
        mode = stat.S_IMODE(info.st_mode)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or mode & 0o007
            or (info.st_uid != os.geteuid() and info.st_gid != os.getegid())
            or any(destination.iterdir())
        ):
            raise HostedCustodyMountUnavailable
        for name in _FILENAMES:
            temporary = destination / f".{name}.{uuid.uuid4().hex}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
                0o600,
            )
            staged.append(temporary)
            try:
                view = memoryview(payloads[name])
                while view:
                    written = os.write(descriptor, view)
                    if written < 1:
                        raise HostedCustodyMountUnavailable
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, destination / name)
            staged.remove(temporary)
        directory_fd = os.open(
            destination,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except HostedCustodyMountUnavailable:
        for path in staged:
            try:
                path.unlink()
            except OSError:
                pass
        for name in _FILENAMES:
            try:
                (destination / name).unlink()
            except OSError:
                pass
        raise
    except (OSError, TypeError, ValueError):
        for path in staged:
            try:
                path.unlink()
            except OSError:
                pass
        for name in _FILENAMES:
            try:
                (destination / name).unlink()
            except OSError:
                pass
        raise HostedCustodyMountUnavailable from None


def main() -> int:
    try:
        copy_projected_custody(SOURCE_ROOT, HOSTED_CUSTODY_ROOT)
    except HostedCustodyMountUnavailable:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
