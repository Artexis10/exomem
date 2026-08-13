"""POSIX implementation of the held, no-follow filesystem primitive."""

from __future__ import annotations

import errno
import os
import secrets
import stat
from pathlib import Path

from .held_fs import (
    Capabilities,
    HeldDirectory,
    HeldFilesystem,
    HeldFsError,
    HeldResult,
    SagaRecord,
    StableIdentity,
)

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_NOFOLLOW", 0
)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_NOFOLLOW", 0
)


def _identity(info: os.stat_result) -> StableIdentity:
    if stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    else:
        kind = "other"
    return StableIdentity(info.st_dev, info.st_ino, kind, info.st_nlink)


def _error(error: OSError) -> HeldFsError:
    if error.errno == errno.EXDEV:
        return HeldFsError("CROSS_DEVICE", "operation requires one filesystem")
    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        return HeldFsError("UNSAFE_PATH", "unsafe filesystem object")
    if error.errno in {errno.ENOENT, errno.ESTALE}:
        return HeldFsError("MISSING", "filesystem object is unavailable")
    return HeldFsError("IO_REFUSED", "held filesystem operation was refused")


def _invalid() -> HeldFsError:
    return HeldFsError("UNSAFE_PATH", "unsafe filesystem object")


def _parts(relative: str) -> tuple[str, ...] | None:
    if relative == ".":
        return ()
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        return None
    parts = tuple(relative.split("/"))
    if any(part in {"", ".", ".."} or "\\" in part for part in parts):
        return None
    return parts


def _leaf(name: str) -> bool:
    return isinstance(name, str) and name not in {"", ".", ".."} and "/" not in name and "\\" not in name and "\x00" not in name


def _probe(root: Path) -> Capabilities:
    if not all((getattr(os, "O_NOFOLLOW", 0), os.open in os.supports_dir_fd, os.rename in os.supports_dir_fd, os.unlink in os.supports_dir_fd)):
        return Capabilities.disabled("descriptor-relative no-follow operations are unavailable")
    try:
        descriptor = os.open(os.fspath(root), _DIRECTORY_FLAGS)
    except OSError:
        return Capabilities.disabled("root cannot be opened as a no-follow directory")
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            return Capabilities.disabled("root is not a directory")
        source = f".exomem-held-probe-{secrets.token_hex(16)}"
        linked = f"{source}-link"
        renamed = f"{source}-renamed"
        try:
            leaf = os.open(
                source,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=descriptor,
            )
            try:
                leaf_identity = os.fstat(leaf)
                if not stat.S_ISREG(leaf_identity.st_mode):
                    return Capabilities.disabled("stable final identity is unavailable")
            finally:
                os.close(leaf)
            try:
                os.link(source, linked, src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
                os.rename(linked, renamed, src_dir_fd=descriptor, dst_dir_fd=descriptor)
                os.unlink(renamed, dir_fd=descriptor)
                hard_link = True
            except OSError:
                hard_link = False
            os.unlink(source, dir_fd=descriptor)
        except OSError:
            return Capabilities.disabled("relative filesystem operations are unavailable")
        return Capabilities(
            relative_operations=True,
            no_follow=True,
            stable_identity=True,
            same_device_rename=True,
            hard_link=hard_link,
        )
    finally:
        for name in (locals().get("source"), locals().get("linked"), locals().get("renamed")):
            if isinstance(name, str):
                try:
                    os.unlink(name, dir_fd=descriptor)
                except OSError:
                    pass
        os.close(descriptor)


def probe(root: Path) -> Capabilities:
    return _probe(root)


class PosixHeldDirectory(HeldDirectory):
    def __init__(self, filesystem: PosixHeldFilesystem, descriptor: int) -> None:
        self.filesystem = filesystem
        self.descriptor = descriptor
        self.identity = _identity(os.fstat(descriptor))
        self.closed = False

    def check(self) -> None:
        if self.closed:
            raise OSError(errno.ESTALE, "held directory is closed")
        current = _identity(os.fstat(self.descriptor))
        if current != self.identity or current.kind != "directory":
            raise OSError(errno.ESTALE, "held directory changed")

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            self.closed = True


class PosixHeldFilesystem(HeldFilesystem):
    def __init__(self, descriptor: int, capabilities: Capabilities) -> None:
        self.descriptor = descriptor
        self.capabilities = capabilities
        self.root_identity = _identity(os.fstat(descriptor))
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            self.closed = True

    def _check(self, parent: HeldDirectory) -> PosixHeldDirectory:
        if self.closed or not isinstance(parent, PosixHeldDirectory) or parent.filesystem is not self:
            raise OSError(errno.ESTALE, "held filesystem is unavailable")
        parent.check()
        return parent

    def parent(self, relative: str, *, create: bool = False) -> HeldResult[HeldDirectory]:
        parts = _parts(relative)
        if parts is None:
            return HeldResult(error=_invalid())
        if self.closed:
            return HeldResult(error=HeldFsError("IO_REFUSED", "held root is closed"))
        descriptor = os.dup(self.descriptor)
        try:
            for part in parts:
                try:
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return HeldResult(value=PosixHeldDirectory(self, descriptor))
        except OSError as error:
            os.close(descriptor)
            return HeldResult(error=_error(error))

    def _open_leaf(self, parent: HeldDirectory, leaf: str, flags: int, mode: int = 0o600) -> int:
        if not _leaf(leaf):
            raise OSError(errno.ELOOP, "invalid relative leaf")
        checked = self._check(parent)
        descriptor = os.open(leaf, flags, mode, dir_fd=checked.descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            os.close(descriptor)
            raise OSError(errno.ELOOP, "leaf is not a regular file")
        return descriptor

    def identity(self, parent: HeldDirectory, leaf: str) -> HeldResult[StableIdentity]:
        try:
            descriptor = self._open_leaf(parent, leaf, _READ_FLAGS)
            try:
                return HeldResult(value=_identity(os.fstat(descriptor)))
            finally:
                os.close(descriptor)
        except OSError as error:
            return HeldResult(error=_error(error))

    def read(self, parent: HeldDirectory, leaf: str) -> HeldResult[bytes]:
        try:
            descriptor = self._open_leaf(parent, leaf, _READ_FLAGS)
            try:
                chunks = []
                while chunk := os.read(descriptor, 65536):
                    chunks.append(chunk)
                return HeldResult(value=b"".join(chunks))
            finally:
                os.close(descriptor)
        except OSError as error:
            return HeldResult(error=_error(error))

    def write(self, parent: HeldDirectory, leaf: str, data: bytes) -> HeldResult[None]:
        if not isinstance(data, bytes):
            return HeldResult(error=HeldFsError("INVALID_INPUT", "data must be bytes"))
        try:
            descriptor = self._open_leaf(parent, leaf, _WRITE_FLAGS)
            try:
                view = memoryview(data)
                written = 0
                while written < len(view):
                    written += os.write(descriptor, view[written:])
                os.fsync(descriptor)
                return HeldResult(value=None)  # type: ignore[arg-type]
            finally:
                os.close(descriptor)
        except OSError as error:
            return HeldResult(error=_error(error))

    def _paired(self, source: HeldDirectory, destination: HeldDirectory, source_leaf: str, destination_leaf: str) -> tuple[PosixHeldDirectory, PosixHeldDirectory]:
        if not _leaf(source_leaf) or not _leaf(destination_leaf):
            raise OSError(errno.ELOOP, "invalid relative leaf")
        source_parent = self._check(source)
        if not isinstance(destination, PosixHeldDirectory):
            raise OSError(errno.ESTALE, "destination handle is unavailable")
        destination.check()
        if source_parent.identity.device != destination.identity.device:
            raise OSError(errno.EXDEV, "parents are on different devices")
        return source_parent, destination

    def rename(self, source_parent: HeldDirectory, source_leaf: str, destination_parent: HeldDirectory, destination_leaf: str) -> HeldResult[None]:
        try:
            source, destination = self._paired(source_parent, destination_parent, source_leaf, destination_leaf)
            os.rename(source_leaf, destination_leaf, src_dir_fd=source.descriptor, dst_dir_fd=destination.descriptor)
            return HeldResult(value=None)  # type: ignore[arg-type]
        except OSError as error:
            return HeldResult(error=_error(error))

    def link(self, source_parent: HeldDirectory, source_leaf: str, destination_parent: HeldDirectory, destination_leaf: str) -> HeldResult[None]:
        if not self.capabilities.hard_link:
            return HeldResult(error=HeldFsError("CAPABILITY_UNAVAILABLE", "hard links are unavailable"))
        try:
            source, destination = self._paired(source_parent, destination_parent, source_leaf, destination_leaf)
            source_descriptor = self._open_leaf(source, source_leaf, _READ_FLAGS)
            os.close(source_descriptor)
            os.link(source_leaf, destination_leaf, src_dir_fd=source.descriptor, dst_dir_fd=destination.descriptor, follow_symlinks=False)
            return HeldResult(value=None)  # type: ignore[arg-type]
        except OSError as error:
            return HeldResult(error=_error(error))

    def unlink(self, parent: HeldDirectory, leaf: str) -> HeldResult[None]:
        try:
            checked = self._check(parent)
            descriptor = self._open_leaf(checked, leaf, _READ_FLAGS)
            os.close(descriptor)
            os.unlink(leaf, dir_fd=checked.descriptor)
            return HeldResult(value=None)  # type: ignore[arg-type]
        except OSError as error:
            return HeldResult(error=_error(error))

    def copy(self, source_parent: HeldDirectory, source_leaf: str, destination_parent: HeldDirectory, destination_leaf: str) -> HeldResult[None]:
        temporary = f".exomem-held-{secrets.token_hex(16)}"
        temporary_descriptor: int | None = None
        try:
            if not _leaf(source_leaf) or not _leaf(destination_leaf):
                raise OSError(errno.ELOOP, "invalid relative leaf")
            source = self._check(source_parent)
            if not isinstance(destination_parent, PosixHeldDirectory):
                raise OSError(errno.ESTALE, "destination handle is unavailable")
            destination_parent.check()
            destination = destination_parent
            source_descriptor = self._open_leaf(source, source_leaf, _READ_FLAGS)
            try:
                temporary_descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=destination.descriptor,
                )
                while chunk := os.read(source_descriptor, 65536):
                    view = memoryview(chunk)
                    written = 0
                    while written < len(view):
                        written += os.write(temporary_descriptor, view[written:])
                os.fsync(temporary_descriptor)
            finally:
                os.close(source_descriptor)
                if temporary_descriptor is not None:
                    os.close(temporary_descriptor)
                    temporary_descriptor = None
            os.replace(temporary, destination_leaf, src_dir_fd=destination.descriptor, dst_dir_fd=destination.descriptor)
            os.fsync(destination.descriptor)
            return HeldResult(value=None)  # type: ignore[arg-type]
        except OSError as error:
            try:
                if temporary_descriptor is not None:
                    os.close(temporary_descriptor)
                if isinstance(destination_parent, PosixHeldDirectory):
                    os.unlink(temporary, dir_fd=destination_parent.descriptor)
            except OSError:
                pass
            return HeldResult(error=_error(error))

    def enumerate(self, parent: HeldDirectory) -> HeldResult[tuple[SagaRecord, ...]]:
        try:
            checked = self._check(parent)
            records: list[SagaRecord] = []
            self._enumerate(checked.descriptor, "", records)
            return HeldResult(value=tuple(records))
        except OSError as error:
            return HeldResult(error=_error(error))

    def _enumerate(self, descriptor: int, prefix: str, records: list[SagaRecord]) -> None:
        with os.scandir(os.dup(descriptor)) as children:
            names = sorted(child.name for child in children)
        for name in names:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                raise OSError(errno.ELOOP, "unsafe recursive entry")
            relative = f"{prefix}/{name}" if prefix else name
            identity = _identity(info)
            if identity.kind not in {"file", "directory"}:
                raise OSError(errno.ELOOP, "unsafe recursive entry")
            records.append(SagaRecord(relative, identity))
            if identity.kind == "directory":
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                try:
                    self._enumerate(child, relative, records)
                finally:
                    os.close(child)


def acquire(root: Path) -> HeldResult[HeldFilesystem]:
    capability = probe(root)
    if not capability.relative_operations:
        return HeldResult(error=HeldFsError("CAPABILITY_UNAVAILABLE", "required filesystem primitives are unavailable"))
    try:
        descriptor = os.open(os.fspath(root), _DIRECTORY_FLAGS)
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            os.close(descriptor)
            return HeldResult(error=_invalid())
        return HeldResult(value=PosixHeldFilesystem(descriptor, capability))
    except OSError as error:
        return HeldResult(error=_error(error))
