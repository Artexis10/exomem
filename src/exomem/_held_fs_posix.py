"""POSIX implementation of the held, no-follow filesystem primitive."""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import stat
import sys
from ctypes import Structure, byref, c_char_p, c_int, c_long, c_size_t, c_uint64
from pathlib import Path

from .held_fs import (
    Capabilities,
    HeldDirectory,
    HeldFile,
    HeldFilesystem,
    HeldFsError,
    HeldResult,
    SagaRecord,
    StableIdentity,
)

RESOLVE_NO_XDEV = 0x01
RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
_OPENAT2_RESOLVE = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_XDEV
_SYS_OPENAT2 = 437

_DIRECTORY_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_WRITE_FLAGS = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


class _OpenHow(Structure):
    _fields_ = [("flags", c_uint64), ("mode", c_uint64), ("resolve", c_uint64)]


_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.syscall.argtypes = None
_LIBC.syscall.restype = c_long


def _identity(info: os.stat_result) -> StableIdentity:
    if stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    else:
        kind = "other"
    return StableIdentity(info.st_dev, info.st_ino, kind, info.st_nlink)


def _same_object(left: StableIdentity, right: StableIdentity) -> bool:
    return (left.device, left.inode, left.kind) == (right.device, right.inode, right.kind)


def _error(error: OSError) -> HeldFsError:
    if error.errno == errno.EXDEV:
        return HeldFsError("CROSS_DEVICE", "operation requires one filesystem")
    if error.errno in {errno.ELOOP, errno.ENOTDIR}:
        return HeldFsError("UNSAFE_PATH", "unsafe filesystem object")
    if error.errno == errno.ESTALE:
        return HeldFsError("IDENTITY_CHANGED", "held filesystem identity changed")
    if error.errno == errno.EEXIST:
        return HeldFsError("DESTINATION_EXISTS", "destination already exists")
    if error.errno == errno.ENOENT:
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
    return (
        isinstance(name, str)
        and name not in {"", ".", ".."}
        and "/" not in name
        and "\\" not in name
        and "\x00" not in name
    )


def _linux_openat2(parent: int, name: str, flags: int, mode: int = 0) -> int:
    how = _OpenHow(flags, mode, _OPENAT2_RESOLVE)
    result = _LIBC.syscall(
        c_long(_SYS_OPENAT2),
        c_int(parent),
        c_char_p(os.fsencode(name)),
        byref(how),
        c_size_t(ctypes.sizeof(how)),
    )
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(result)


def _open_relative(parent: int, name: str, flags: int, mode: int = 0o600) -> int:
    if sys.platform.startswith("linux"):
        return _linux_openat2(parent, name, flags, mode if flags & os.O_CREAT else 0)
    descriptor = os.open(name, flags, mode, dir_fd=parent)
    if os.fstat(descriptor).st_dev != os.fstat(parent).st_dev:
        os.close(descriptor)
        raise OSError(errno.EXDEV, "mount traversal is unavailable")
    return descriptor


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
        if not _same_object(current, self.identity) or current.kind != "directory":
            raise OSError(errno.ESTALE, "held directory changed")

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            self.closed = True


class PosixHeldFile(HeldFile):
    def __init__(
        self,
        filesystem: PosixHeldFilesystem,
        parent_descriptor: int,
        parent_identity: StableIdentity,
        name: str,
        descriptor: int,
        access: str,
    ) -> None:
        self.filesystem = filesystem
        self.parent_descriptor = parent_descriptor
        self.parent_identity = parent_identity
        self.name = name
        self.descriptor = descriptor
        self.access = access
        self.identity = _identity(os.fstat(descriptor))
        self.closed = False
        self.named = True

    def check(self) -> None:
        if self.closed or self.filesystem.closed:
            raise OSError(errno.ESTALE, "held file is closed")
        current = _identity(os.fstat(self.descriptor))
        if current != self.identity or current.kind != "file":
            raise OSError(errno.ESTALE, "held file changed")
        if not _same_object(_identity(os.fstat(self.parent_descriptor)), self.parent_identity):
            raise OSError(errno.ESTALE, "held parent changed")

    def check_name(self) -> None:
        self.check()
        if not self.named:
            raise OSError(errno.ESTALE, "held name is no longer current")
        current = _identity(
            os.stat(self.name, dir_fd=self.parent_descriptor, follow_symlinks=False)
        )
        if current != self.identity:
            raise OSError(errno.ESTALE, "held name identity changed")

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            os.close(self.parent_descriptor)
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

    def _check_directory(self, parent: HeldDirectory) -> PosixHeldDirectory:
        if (
            self.closed
            or not isinstance(parent, PosixHeldDirectory)
            or parent.filesystem is not self
        ):
            raise OSError(errno.ESTALE, "held filesystem is unavailable")
        parent.check()
        return parent

    def _check_file(self, file: HeldFile) -> PosixHeldFile:
        if not isinstance(file, PosixHeldFile) or file.filesystem is not self:
            raise OSError(errno.ESTALE, "held file is unavailable")
        file.check()
        return file

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
                    child = _open_relative(descriptor, part, _DIRECTORY_FLAGS)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, 0o700, dir_fd=descriptor)
                    child = _open_relative(descriptor, part, _DIRECTORY_FLAGS)
                os.close(descriptor)
                descriptor = child
            return HeldResult(value=PosixHeldDirectory(self, descriptor))
        except OSError as error:
            os.close(descriptor)
            return HeldResult(error=_error(error))

    def file(
        self,
        parent: HeldDirectory,
        leaf: str,
        *,
        access: str = "read",
        create: bool = False,
        exclusive: bool = False,
    ) -> HeldResult[HeldFile]:
        if not _leaf(leaf) or access not in {"read", "write", "mutate"}:
            return HeldResult(error=_invalid())
        if exclusive and not create:
            return HeldResult(error=HeldFsError("INVALID_INPUT", "exclusive requires create"))
        parent_descriptor: int | None = None
        descriptor: int | None = None
        try:
            checked = self._check_directory(parent)
            parent_descriptor = os.dup(checked.descriptor)
            flags = _READ_FLAGS if access in {"read", "mutate"} else _WRITE_FLAGS
            if create:
                flags |= os.O_CREAT
            if exclusive:
                flags |= os.O_EXCL
            descriptor = _open_relative(parent_descriptor, leaf, flags)
            identity = _identity(os.fstat(descriptor))
            if identity.kind != "file":
                raise OSError(errno.ELOOP, "leaf is not a regular file")
            return HeldResult(
                value=PosixHeldFile(
                    self,
                    parent_descriptor,
                    checked.identity,
                    leaf,
                    descriptor,
                    access,
                )
            )
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            if parent_descriptor is not None:
                os.close(parent_descriptor)
            return HeldResult(error=_error(error))

    def read(self, file: HeldFile) -> HeldResult[bytes]:
        try:
            checked = self._check_file(file)
            os.lseek(checked.descriptor, 0, os.SEEK_SET)
            chunks = []
            while chunk := os.read(checked.descriptor, 65536):
                chunks.append(chunk)
            return HeldResult(value=b"".join(chunks))
        except OSError as error:
            return HeldResult(error=_error(error))

    def write(self, file: HeldFile, data: bytes) -> HeldResult[None]:
        if not isinstance(data, bytes):
            return HeldResult(error=HeldFsError("INVALID_INPUT", "data must be bytes"))
        try:
            checked = self._check_file(file)
            if checked.access != "write":
                return HeldResult(error=HeldFsError("INVALID_ACCESS", "held file is not writable"))
            os.ftruncate(checked.descriptor, 0)
            os.lseek(checked.descriptor, 0, os.SEEK_SET)
            view = memoryview(data)
            written = 0
            while written < len(view):
                written += os.write(checked.descriptor, view[written:])
            os.fsync(checked.descriptor)
            checked.identity = _identity(os.fstat(checked.descriptor))
            return HeldResult(value=None)
        except OSError as error:
            return HeldResult(error=_error(error))

    def _destination(
        self, source: PosixHeldFile, destination: HeldDirectory, destination_leaf: str
    ) -> PosixHeldDirectory:
        if source.access != "mutate" or not _leaf(destination_leaf):
            raise OSError(errno.ELOOP, "invalid held mutation")
        source.check_name()
        if not isinstance(destination, PosixHeldDirectory):
            raise OSError(errno.ESTALE, "destination handle is unavailable")
        destination.check()
        if source.identity.device != destination.identity.device:
            raise OSError(errno.EXDEV, "parents are on different devices")
        if destination.filesystem is not self:
            raise OSError(errno.ESTALE, "destination belongs to another root")
        try:
            os.stat(destination_leaf, dir_fd=destination.descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise OSError(errno.EEXIST, "destination exists")
        return destination

    def rename(
        self, source: HeldFile, destination_parent: HeldDirectory, destination_leaf: str
    ) -> HeldResult[None]:
        try:
            checked = self._check_file(source)
            destination = self._destination(checked, destination_parent, destination_leaf)
            os.rename(
                checked.name,
                destination_leaf,
                src_dir_fd=checked.parent_descriptor,
                dst_dir_fd=destination.descriptor,
            )
            checked.named = False
            return HeldResult(value=None)
        except OSError as error:
            return HeldResult(error=_error(error))

    def link(
        self, source: HeldFile, destination_parent: HeldDirectory, destination_leaf: str
    ) -> HeldResult[None]:
        if not self.capabilities.hard_link:
            return HeldResult(
                error=HeldFsError("CAPABILITY_UNAVAILABLE", "hard links are unavailable")
            )
        try:
            checked = self._check_file(source)
            destination = self._destination(checked, destination_parent, destination_leaf)
            os.link(
                checked.name,
                destination_leaf,
                src_dir_fd=checked.parent_descriptor,
                dst_dir_fd=destination.descriptor,
                follow_symlinks=False,
            )
            checked.identity = _identity(os.fstat(checked.descriptor))
            return HeldResult(value=None)
        except OSError as error:
            return HeldResult(error=_error(error))

    def unlink(self, file: HeldFile) -> HeldResult[None]:
        try:
            checked = self._check_file(file)
            if checked.access != "mutate":
                return HeldResult(error=HeldFsError("INVALID_ACCESS", "held file is not mutable"))
            checked.check_name()
            os.unlink(checked.name, dir_fd=checked.parent_descriptor)
            checked.named = False
            return HeldResult(value=None)
        except OSError as error:
            return HeldResult(error=_error(error))

    def copy(
        self, source: HeldFile, destination_parent: HeldDirectory, destination_leaf: str
    ) -> HeldResult[None]:
        temporary = f".exomem-held-{secrets.token_hex(16)}"
        temporary_descriptor: int | None = None
        destination: PosixHeldDirectory | None = None
        try:
            checked = self._check_file(source)
            if not _leaf(destination_leaf):
                raise OSError(errno.ELOOP, "invalid destination leaf")
            if not isinstance(destination_parent, PosixHeldDirectory):
                raise OSError(errno.ESTALE, "destination handle is unavailable")
            destination_parent.check()
            if destination_parent.filesystem.closed:
                raise OSError(errno.ESTALE, "destination root is unavailable")
            destination = destination_parent
            try:
                os.stat(
                    destination_leaf,
                    dir_fd=destination.descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise OSError(errno.EEXIST, "destination exists")

            os.lseek(checked.descriptor, 0, os.SEEK_SET)
            temporary_descriptor = _open_relative(
                destination.descriptor,
                temporary,
                _WRITE_FLAGS | os.O_CREAT | os.O_EXCL,
            )
            while chunk := os.read(checked.descriptor, 65536):
                view = memoryview(chunk)
                written = 0
                while written < len(view):
                    written += os.write(temporary_descriptor, view[written:])
            os.fsync(temporary_descriptor)
            os.close(temporary_descriptor)
            temporary_descriptor = None
            os.rename(
                temporary,
                destination_leaf,
                src_dir_fd=destination.descriptor,
                dst_dir_fd=destination.descriptor,
            )
            os.fsync(destination.descriptor)
            return HeldResult(value=None)
        except OSError as error:
            if temporary_descriptor is not None:
                os.close(temporary_descriptor)
            if destination is not None:
                try:
                    os.unlink(temporary, dir_fd=destination.descriptor)
                except OSError:
                    pass
            return HeldResult(error=_error(error))

    def enumerate(self, parent: HeldDirectory) -> HeldResult[tuple[SagaRecord, ...]]:
        try:
            checked = self._check_directory(parent)
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
            flags = _DIRECTORY_FLAGS if stat.S_ISDIR(info.st_mode) else _READ_FLAGS
            child = _open_relative(descriptor, name, flags)
            try:
                identity = _identity(os.fstat(child))
                if identity.kind not in {"file", "directory"} or identity != _identity(info):
                    raise OSError(errno.ESTALE, "recursive entry changed")
                relative = f"{prefix}/{name}" if prefix else name
                records.append(SagaRecord(relative, identity))
                if identity.kind == "directory":
                    self._enumerate(child, relative, records)
            finally:
                os.close(child)


def _probe(root: Path) -> Capabilities:
    if not sys.platform.startswith("linux"):
        return Capabilities.disabled("mount-aware relative operations are unavailable")
    if not all(
        (
            getattr(os, "O_NOFOLLOW", 0),
            os.open in os.supports_dir_fd,
            os.rename in os.supports_dir_fd,
            os.unlink in os.supports_dir_fd,
        )
    ):
        return Capabilities.disabled("descriptor-relative no-follow operations are unavailable")
    try:
        descriptor = os.open(os.fspath(root), _DIRECTORY_FLAGS)
    except OSError:
        return Capabilities.disabled("root cannot be opened as a no-follow directory")

    provisional = Capabilities(True, True, True, True, True)
    filesystem = PosixHeldFilesystem(descriptor, provisional)
    token = secrets.token_hex(16)
    source_name = f".exomem-held-probe-{token}"
    renamed_name = f"{source_name}-renamed"
    linked_name = f"{source_name}-link"
    alias_name = f"{source_name}-alias"
    directory_name = f"{source_name}-directory"
    directory_alias_name = f"{directory_name}-alias"
    try:
        with filesystem.parent(".").require() as parent:
            with filesystem.file(
                parent, source_name, access="write", create=True, exclusive=True
            ).require() as source:
                if not filesystem.write(source, b"probe").ok:
                    return Capabilities.disabled("relative create/write is unavailable")

            os.symlink(source_name, alias_name, dir_fd=parent.descriptor)
            alias = filesystem.file(parent, alias_name)
            if alias.ok:
                alias.require().close()
                return Capabilities.disabled("final no-follow refusal is unavailable")
            os.mkdir(directory_name, 0o700, dir_fd=parent.descriptor)
            os.symlink(
                directory_name,
                directory_alias_name,
                target_is_directory=True,
                dir_fd=parent.descriptor,
            )
            alias_parent = filesystem.parent(directory_alias_name)
            if alias_parent.ok:
                alias_parent.require().close()
                return Capabilities.disabled("parent no-follow refusal is unavailable")

            with filesystem.file(parent, source_name, access="mutate").require() as source:
                hard_link = filesystem.link(source, parent, linked_name).ok
                renamed = filesystem.rename(source, parent, renamed_name)
                if not renamed.ok:
                    return Capabilities.disabled("same-device relative rename is unavailable")
            with filesystem.file(parent, renamed_name, access="mutate").require() as renamed:
                if not filesystem.unlink(renamed).ok:
                    return Capabilities.disabled("relative unlink is unavailable")
        return Capabilities(True, True, True, True, hard_link)
    except (HeldFsError, OSError):
        return Capabilities.disabled("actual-filesystem capability probe failed")
    finally:
        try:
            root_descriptor = filesystem.descriptor
            for name in (
                source_name,
                renamed_name,
                linked_name,
                alias_name,
                directory_alias_name,
            ):
                try:
                    os.unlink(name, dir_fd=root_descriptor)
                except OSError:
                    pass
            try:
                os.rmdir(directory_name, dir_fd=root_descriptor)
            except OSError:
                pass
        finally:
            filesystem.close()


def probe(root: Path) -> Capabilities:
    return _probe(root)


def acquire(root: Path) -> HeldResult[HeldFilesystem]:
    capability = probe(root)
    if not capability.relative_operations:
        return HeldResult(
            error=HeldFsError(
                "CAPABILITY_UNAVAILABLE", "required filesystem primitives are unavailable"
            )
        )
    try:
        descriptor = os.open(os.fspath(root), _DIRECTORY_FLAGS)
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            os.close(descriptor)
            return HeldResult(error=_invalid())
        return HeldResult(value=PosixHeldFilesystem(descriptor, capability))
    except OSError as error:
        return HeldResult(error=_error(error))
