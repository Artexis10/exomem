"""Native Windows held-handle implementation.

Descendants are opened with ``NtCreateFile`` relative to a retained directory
handle. There is deliberately no reconstructed-descendant-path fallback.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import secrets
from ctypes import (
    POINTER,
    Structure,
    Union,
    byref,
    c_byte,
    c_long,
    c_ubyte,
    c_uint32,
    c_ulong,
    c_ulonglong,
    c_ushort,
    c_void_p,
    create_string_buffer,
    create_unicode_buffer,
    sizeof,
    wintypes,
)
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

FILE_OPEN = 1
FILE_CREATE = 2
FILE_OPEN_IF = 3
FILE_DIRECTORY_FILE = 0x00000001
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
FILE_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
FILE_SHARE_ALL = FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
FILE_LIST_DIRECTORY = 0x00000001
FILE_ADD_FILE = 0x00000002
FILE_ADD_SUBDIRECTORY = 0x00000004
FILE_READ_DATA = 0x00000001
FILE_WRITE_DATA = 0x00000002
FILE_READ_ATTRIBUTES = 0x00000080
FILE_WRITE_ATTRIBUTES = 0x00000100
GENERIC_WRITE = 0x40000000
READ_CONTROL = 0x00020000
DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
OBJ_CASE_INSENSITIVE = 0x00000040
FILE_RENAME_INFORMATION = 10
FILE_RENAME_INFORMATION_EX = 65
FILE_RENAME_REPLACE_IF_EXISTS = 0x00000001
FILE_RENAME_POSIX_SEMANTICS = 0x00000002
FILE_LINK_INFORMATION = 11
FILE_DISPOSITION_INFORMATION = 4
FILE_BOTH_DIRECTORY_INFORMATION = 3
FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
FILE_ID_INFO_CLASS = 18
INVALID_HANDLE_VALUE = c_void_p(-1).value
STATUS_NO_MORE_FILES = 0x80000006


class UNICODE_STRING(Structure):
    _fields_ = [("Length", c_ushort), ("MaximumLength", c_ushort), ("Buffer", wintypes.LPWSTR)]


class OBJECT_ATTRIBUTES(Structure):
    _fields_ = [
        ("Length", c_ulong),
        ("RootDirectory", c_void_p),
        ("ObjectName", POINTER(UNICODE_STRING)),
        ("Attributes", c_ulong),
        ("SecurityDescriptor", c_void_p),
        ("SecurityQualityOfService", c_void_p),
    ]


class IO_STATUS_BLOCK(Structure):
    _fields_ = [("Status", c_void_p), ("Information", c_void_p)]


class FILE_ATTRIBUTE_TAG_INFO(Structure):
    _fields_ = [("FileAttributes", c_ulong), ("ReparseTag", c_ulong)]


class FILE_ID_INFO(Structure):
    _fields_ = [("VolumeSerialNumber", c_ulonglong), ("FileId", c_byte * 16)]


class BY_HANDLE_FILE_INFORMATION(Structure):
    _fields_ = [
        ("FileAttributes", c_ulong),
        ("CreationTimeLow", c_ulong),
        ("CreationTimeHigh", c_ulong),
        ("LastAccessTimeLow", c_ulong),
        ("LastAccessTimeHigh", c_ulong),
        ("LastWriteTimeLow", c_ulong),
        ("LastWriteTimeHigh", c_ulong),
        ("VolumeSerialNumber", c_ulong),
        ("FileSizeHigh", c_ulong),
        ("FileSizeLow", c_ulong),
        ("NumberOfLinks", c_ulong),
        ("FileIndexHigh", c_ulong),
        ("FileIndexLow", c_ulong),
    ]


class FILE_NAME_OPTIONS(Union):
    _fields_ = [("ReplaceIfExists", c_ubyte), ("Flags", c_uint32)]


class FILE_NAME_INFORMATION(Structure):
    """ABI-derived header shared by native rename and link information."""

    _fields_ = [
        ("Options", FILE_NAME_OPTIONS),
        ("RootDirectory", c_void_p),
        ("FileNameLength", c_uint32),
        ("FileName", c_ushort * 1),
    ]


class FILE_DISPOSITION_INFO(Structure):
    _fields_ = [("DeleteFile", c_ubyte)]


if os.name == "nt":  # pragma: no cover - executed by windows-latest
    NtCreateFile = ctypes.windll.ntdll.NtCreateFile
    NtSetInformationFile = ctypes.windll.ntdll.NtSetInformationFile
    NtQueryDirectoryFile = ctypes.windll.ntdll.NtQueryDirectoryFile
    RtlNtStatusToDosError = ctypes.windll.ntdll.RtlNtStatusToDosError
    SetFileInformationByHandle = ctypes.windll.kernel32.SetFileInformationByHandle
    GetFileInformationByHandleEx = ctypes.windll.kernel32.GetFileInformationByHandleEx
    GetFileInformationByHandle = ctypes.windll.kernel32.GetFileInformationByHandle
    CreateFileW = ctypes.windll.kernel32.CreateFileW
    CloseHandle = ctypes.windll.kernel32.CloseHandle
    FlushFileBuffers = ctypes.windll.kernel32.FlushFileBuffers

    NtCreateFile.argtypes = [
        POINTER(c_void_p),
        c_ulong,
        POINTER(OBJECT_ATTRIBUTES),
        POINTER(IO_STATUS_BLOCK),
        c_void_p,
        c_ulong,
        c_ulong,
        c_ulong,
        c_ulong,
        c_void_p,
        c_ulong,
    ]
    NtCreateFile.restype = c_long
    NtSetInformationFile.argtypes = [
        c_void_p,
        POINTER(IO_STATUS_BLOCK),
        c_void_p,
        c_ulong,
        c_ulong,
    ]
    NtSetInformationFile.restype = c_long
    NtQueryDirectoryFile.argtypes = [
        c_void_p,
        c_void_p,
        c_void_p,
        c_void_p,
        POINTER(IO_STATUS_BLOCK),
        c_void_p,
        c_ulong,
        c_ulong,
        wintypes.BOOL,
        c_void_p,
        wintypes.BOOL,
    ]
    NtQueryDirectoryFile.restype = c_long
    RtlNtStatusToDosError.argtypes = [c_long]
    RtlNtStatusToDosError.restype = c_ulong
    SetFileInformationByHandle.argtypes = [c_void_p, c_ulong, c_void_p, c_ulong]
    SetFileInformationByHandle.restype = wintypes.BOOL
    GetFileInformationByHandleEx.argtypes = [c_void_p, c_ulong, c_void_p, c_ulong]
    GetFileInformationByHandleEx.restype = wintypes.BOOL
    GetFileInformationByHandle.argtypes = [c_void_p, POINTER(BY_HANDLE_FILE_INFORMATION)]
    GetFileInformationByHandle.restype = wintypes.BOOL
    CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        c_ulong,
        c_ulong,
        c_void_p,
        c_ulong,
        c_ulong,
        c_void_p,
    ]
    CreateFileW.restype = c_void_p
    CloseHandle.argtypes = [c_void_p]
    CloseHandle.restype = wintypes.BOOL
    FlushFileBuffers.argtypes = [c_void_p]
    FlushFileBuffers.restype = wintypes.BOOL
else:  # pragma: no cover - keeps Linux imports syntactically safe
    NtCreateFile = None
    NtSetInformationFile = None
    NtQueryDirectoryFile = None
    RtlNtStatusToDosError = None
    SetFileInformationByHandle = None
    GetFileInformationByHandleEx = None
    GetFileInformationByHandle = None
    CreateFileW = None
    CloseHandle = None
    FlushFileBuffers = None


def _invalid() -> HeldFsError:
    return HeldFsError("UNSAFE_PATH", "unsafe filesystem object")


def _error(error: OSError | None = None) -> HeldFsError:
    if error is not None and error.errno in {17, errno.EXDEV}:
        return HeldFsError("CROSS_DEVICE", "operation requires one filesystem", error)
    if error is not None and error.errno in {errno.ELOOP, errno.ENOTDIR}:
        return HeldFsError("UNSAFE_PATH", "unsafe filesystem object", error)
    if error is not None and error.errno == errno.ESTALE:
        return HeldFsError("IDENTITY_CHANGED", "held filesystem identity changed", error)
    if error is not None and error.errno in {2, 3, errno.ENOENT}:
        return HeldFsError("MISSING", "filesystem object is unavailable", error)
    if error is not None and error.errno in {80, 183, errno.EEXIST}:
        return HeldFsError("DESTINATION_EXISTS", "destination already exists", error)
    return HeldFsError("IO_REFUSED", "held filesystem operation was refused", error)


def _parts(relative: str) -> tuple[str, ...] | None:
    if relative == ".":
        return ()
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        return None
    parts = tuple(relative.split("/"))
    if any(part in {"", ".", ".."} or "\\" in part or ":" in part for part in parts):
        return None
    return parts


def _leaf(name: str) -> bool:
    return (
        isinstance(name, str)
        and name not in {"", ".", ".."}
        and all(marker not in name for marker in ("/", "\\", ":", "\x00"))
    )


def _native(fd: int) -> int:
    import msvcrt

    return int(msvcrt.get_osfhandle(fd))


def _fd(handle: int, access: str = "read") -> int:
    import msvcrt

    flags = os.O_BINARY | (os.O_RDWR if access == "write" else os.O_RDONLY)
    try:
        return msvcrt.open_osfhandle(handle, flags)
    except BaseException as error:
        assert CloseHandle is not None
        CloseHandle(c_void_p(handle))
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        raise OSError("native handle could not become a CRT descriptor") from error


def _nt_success(status: int) -> bool:
    return status >= 0


def _raise_nt(status: int, message: str) -> None:
    code = int(RtlNtStatusToDosError(status)) if RtlNtStatusToDosError is not None else 1
    raise OSError(code, message)


def _identity(handle: int) -> StableIdentity:
    assert GetFileInformationByHandleEx is not None
    assert GetFileInformationByHandle is not None
    file_id = FILE_ID_INFO()
    attributes = FILE_ATTRIBUTE_TAG_INFO()
    basic = BY_HANDLE_FILE_INFORMATION()
    if not GetFileInformationByHandleEx(
        handle, FILE_ID_INFO_CLASS, byref(file_id), sizeof(file_id)
    ):
        raise OSError("FileIdInfo is unavailable")
    if not GetFileInformationByHandleEx(
        handle,
        FILE_ATTRIBUTE_TAG_INFO_CLASS,
        byref(attributes),
        sizeof(attributes),
    ):
        raise OSError("FileAttributeTagInfo is unavailable")
    if attributes.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
        raise OSError(errno.ELOOP, "reparse point")
    if not GetFileInformationByHandle(handle, byref(basic)):
        raise OSError("file identity is unavailable")
    kind = "directory" if attributes.FileAttributes & 0x10 else "file"
    return StableIdentity(
        int(file_id.VolumeSerialNumber),
        int.from_bytes(bytes(file_id.FileId), "little"),
        kind,
        int(basic.NumberOfLinks),
    )


def _same_object(left: StableIdentity, right: StableIdentity) -> bool:
    return (left.device, left.inode, left.kind) == (right.device, right.inode, right.kind)


def _unicode(name: str) -> tuple[UNICODE_STRING, object]:
    buffer = create_unicode_buffer(name)
    value = UNICODE_STRING(
        len(name.encode("utf-16-le")),
        (len(name) + 1) * 2,
        ctypes.cast(buffer, wintypes.LPWSTR),
    )
    return value, buffer


def _open_relative(
    parent_handle: int,
    name: str,
    access: int,
    options: int,
    disposition: int,
) -> int:
    """Open exactly one component under ``parent_handle`` through NtCreateFile."""
    assert NtCreateFile is not None
    string, _buffer = _unicode(name)
    attributes = OBJECT_ATTRIBUTES(
        sizeof(OBJECT_ATTRIBUTES),
        c_void_p(parent_handle),
        ctypes.pointer(string),
        OBJ_CASE_INSENSITIVE,
        None,
        None,
    )
    status = IO_STATUS_BLOCK()
    handle = c_void_p()
    result = NtCreateFile(
        byref(handle),
        access | SYNCHRONIZE,
        byref(attributes),
        byref(status),
        None,
        FILE_ATTRIBUTE_NORMAL,
        FILE_SHARE_ALL,
        disposition,
        options | FILE_OPEN_REPARSE_POINT,
        None,
        0,
    )
    if not _nt_success(result) or handle.value in {None, INVALID_HANDLE_VALUE}:
        _raise_nt(result, "NtCreateFile refused relative handle operation")
    try:
        _identity(int(handle.value))
        return int(handle.value)
    except OSError:
        assert CloseHandle is not None
        CloseHandle(handle)
        raise


def _name_payload(
    replace: bool,
    destination_handle: int,
    leaf: str,
    *,
    information_class: int = FILE_RENAME_INFORMATION,
) -> ctypes.Array[ctypes.c_char]:
    encoded = leaf.encode("utf-16-le")
    offset = FILE_NAME_INFORMATION.FileName.offset
    payload = create_string_buffer(sizeof(FILE_NAME_INFORMATION) + len(encoded))
    information = FILE_NAME_INFORMATION.from_buffer(payload)
    if information_class == FILE_RENAME_INFORMATION_EX:
        information.Options.Flags = (
            FILE_RENAME_REPLACE_IF_EXISTS | FILE_RENAME_POSIX_SEMANTICS
            if replace
            else 0
        )
    else:
        information.Options.ReplaceIfExists = replace
    information.RootDirectory = destination_handle
    information.FileNameLength = len(encoded)
    ctypes.memmove(ctypes.addressof(payload) + offset, encoded, len(encoded))
    return payload


def _set_name_information(
    descriptor: int,
    destination_handle: int,
    leaf: str,
    information_class: int,
    *,
    replace: bool = False,
) -> None:
    assert NtSetInformationFile is not None
    payload = _name_payload(
        replace,
        destination_handle,
        leaf,
        information_class=information_class,
    )
    status = IO_STATUS_BLOCK()
    result = NtSetInformationFile(
        _native(descriptor),
        byref(status),
        payload,
        len(payload),
        information_class,
    )
    if not _nt_success(result):
        _raise_nt(result, "native relative name mutation was refused")


def _delete_descriptor(descriptor: int) -> None:
    assert SetFileInformationByHandle is not None
    disposition = FILE_DISPOSITION_INFO(True)
    if not SetFileInformationByHandle(
        _native(descriptor),
        FILE_DISPOSITION_INFORMATION,
        byref(disposition),
        sizeof(disposition),
    ):
        raise OSError(ctypes.get_last_error(), "relative disposition was refused")


def _open_root(root: Path) -> int | None:
    assert CreateFileW is not None
    handle = CreateFileW(
        str(root),
        FILE_LIST_DIRECTORY | FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY | FILE_READ_ATTRIBUTES,
        FILE_SHARE_ALL,
        None,
        3,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_OPEN_REPARSE_POINT,
        None,
    )
    if handle in {None, INVALID_HANDLE_VALUE}:
        return None
    try:
        identity = _identity(int(handle))
        if identity.kind != "directory":
            raise OSError("root is not a directory")
        return _fd(int(handle))
    except OSError:
        assert CloseHandle is not None
        CloseHandle(c_void_p(handle))
        return None


class WindowsHeldDirectory(HeldDirectory):
    def __init__(
        self,
        filesystem: WindowsHeldFilesystem,
        descriptor: int,
        *,
        parent_descriptor: int | None = None,
        parent_identity: StableIdentity | None = None,
        name: str | None = None,
        access: str = "read",
    ) -> None:
        self.filesystem = filesystem
        self.descriptor = descriptor
        self.parent_descriptor = parent_descriptor
        self.parent_identity = parent_identity
        self.name = name
        self.access = access
        self.identity = _identity(_native(descriptor))
        self.closed = False
        self.named = name is not None

    def check(self) -> None:
        if (
            self.closed
            or self.filesystem.closed
            or not _same_object(_identity(_native(self.descriptor)), self.identity)
        ):
            raise OSError(errno.ESTALE, "held directory changed")
        if self.parent_descriptor is not None and (
            self.parent_identity is None
            or not _same_object(
                _identity(_native(self.parent_descriptor)), self.parent_identity
            )
        ):
            raise OSError(errno.ESTALE, "held directory parent changed")

    def check_name(self) -> None:
        self.check()
        if not self.named or self.parent_descriptor is None or self.name is None:
            raise OSError(errno.ESTALE, "held directory name is unavailable")
        handle = _open_relative(
            _native(self.parent_descriptor),
            self.name,
            FILE_READ_ATTRIBUTES,
            FILE_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT,
            FILE_OPEN,
        )
        try:
            if not _same_object(_identity(handle), self.identity):
                raise OSError(errno.ESTALE, "held directory name identity changed")
        finally:
            assert CloseHandle is not None
            CloseHandle(c_void_p(handle))

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            if self.parent_descriptor is not None:
                os.close(self.parent_descriptor)
            self.closed = True


class WindowsHeldFile(HeldFile):
    def __init__(
        self,
        filesystem: WindowsHeldFilesystem,
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
        self.identity = _identity(_native(descriptor))
        self.closed = False
        self.named = True

    def check(self) -> None:
        if self.closed or self.filesystem.closed:
            raise OSError(errno.ESTALE, "held file is closed")
        if _identity(_native(self.descriptor)) != self.identity:
            raise OSError(errno.ESTALE, "held file changed")
        if not _same_object(_identity(_native(self.parent_descriptor)), self.parent_identity):
            raise OSError(errno.ESTALE, "held parent changed")

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            os.close(self.parent_descriptor)
            self.closed = True


class WindowsHeldFilesystem(HeldFilesystem):
    def __init__(self, descriptor: int, capabilities: Capabilities) -> None:
        self.descriptor = descriptor
        self.capabilities = capabilities
        self.root_identity = _identity(_native(descriptor))
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            self.closed = True

    def _check_directory(self, parent: HeldDirectory) -> WindowsHeldDirectory:
        if (
            self.closed
            or not isinstance(parent, WindowsHeldDirectory)
            or parent.filesystem is not self
        ):
            raise OSError(errno.ESTALE, "held filesystem is unavailable")
        parent.check()
        return parent

    def _check_file(self, file: HeldFile) -> WindowsHeldFile:
        if not isinstance(file, WindowsHeldFile) or file.filesystem is not self:
            raise OSError(errno.ESTALE, "held file is unavailable")
        file.check()
        return file

    def parent(
        self,
        relative: str,
        *,
        create: bool = False,
        exclusive: bool = False,
        access: str = "read",
    ) -> HeldResult[HeldDirectory]:
        parts = _parts(relative)
        if parts is None or access not in {"read", "flush", "mutate"}:
            return HeldResult(error=_invalid())
        if exclusive and (not create or not parts):
            return HeldResult(error=_invalid())
        if self.closed:
            return HeldResult(error=HeldFsError("IO_REFUSED", "held root is closed"))
        if not parts and access != "read":
            return HeldResult(
                error=HeldFsError(
                    "INVALID_INPUT", "elevated root directory access is unsupported"
                )
            )
        descriptor: int | None = None
        parent_descriptor: int | None = None
        try:
            descriptor = os.dup(self.descriptor)
            for index, part in enumerate(parts):
                desired = (
                    FILE_LIST_DIRECTORY
                    | FILE_ADD_FILE
                    | FILE_ADD_SUBDIRECTORY
                    | FILE_READ_ATTRIBUTES
                )
                if index == len(parts) - 1 and access in {"flush", "mutate"}:
                    desired |= GENERIC_WRITE
                if index == len(parts) - 1 and access == "mutate":
                    desired |= DELETE
                handle = _open_relative(
                    _native(descriptor),
                    part,
                    desired,
                    FILE_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT,
                    (
                        FILE_CREATE
                        if exclusive and index == len(parts) - 1
                        else FILE_OPEN
                        if exclusive
                        else FILE_OPEN_IF
                        if create
                        else FILE_OPEN
                    ),
                )
                child = _fd(handle)
                if index == len(parts) - 1:
                    parent_descriptor = descriptor
                else:
                    os.close(descriptor)
                descriptor = child
            return HeldResult(
                value=WindowsHeldDirectory(
                    self,
                    descriptor,
                    parent_descriptor=parent_descriptor,
                    parent_identity=(
                        _identity(_native(parent_descriptor))
                        if parent_descriptor is not None
                        else None
                    ),
                    name=parts[-1] if parts else None,
                    access=access,
                )
            )
        except OSError as error:
            if descriptor is not None:
                os.close(descriptor)
            if parent_descriptor is not None:
                os.close(parent_descriptor)
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
            desired = FILE_READ_DATA | FILE_READ_ATTRIBUTES
            descriptor_access = "read"
            if access == "write":
                desired |= FILE_WRITE_DATA | FILE_WRITE_ATTRIBUTES | READ_CONTROL
                descriptor_access = "write"
            if access == "mutate":
                desired |= DELETE
            disposition = FILE_CREATE if exclusive else FILE_OPEN_IF if create else FILE_OPEN
            handle = _open_relative(
                _native(parent_descriptor),
                leaf,
                desired,
                FILE_NON_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT,
                disposition,
            )
            descriptor = _fd(handle, descriptor_access)
            return HeldResult(
                value=WindowsHeldFile(
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

    def validate_directory(
        self,
        directory: HeldDirectory,
        *,
        require_name: bool = True,
    ) -> HeldResult[None]:
        try:
            checked = self._check_directory(directory)
            if require_name:
                checked.check_name()
            return HeldResult(value=None)
        except OSError as error:
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

    def sha256(self, file: HeldFile) -> HeldResult[str]:
        try:
            checked = self._check_file(file)
            before = os.fstat(checked.descriptor)
            os.lseek(checked.descriptor, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            while chunk := os.read(checked.descriptor, 65536):
                digest.update(chunk)
            after = os.fstat(checked.descriptor)
            if (
                _identity(_native(checked.descriptor)) != checked.identity
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
            ):
                raise OSError(errno.ESTALE, "held file changed while hashing")
            checked.check()
            return HeldResult(value=digest.hexdigest())
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
            checked.identity = _identity(_native(checked.descriptor))
            return HeldResult(value=None)
        except OSError as error:
            return HeldResult(error=_error(error))

    def flush_directory(self, directory: HeldDirectory) -> HeldResult[None]:
        try:
            checked = self._check_directory(directory)
            if checked.access not in {"flush", "mutate"}:
                return HeldResult(
                    error=HeldFsError(
                        "INVALID_ACCESS", "held directory is not flush-capable"
                    )
                )
            assert FlushFileBuffers is not None
            if not FlushFileBuffers(c_void_p(_native(checked.descriptor))):
                raise OSError(ctypes.get_last_error(), "directory flush was refused")
            return HeldResult(value=None)
        except OSError as error:
            return HeldResult(error=_error(error))

    def _destination(
        self,
        source: WindowsHeldFile,
        destination: HeldDirectory,
        destination_leaf: str,
        *,
        replace: bool,
    ) -> WindowsHeldDirectory:
        if source.access != "mutate" or not _leaf(destination_leaf):
            raise OSError(errno.ELOOP, "invalid held mutation")
        if not isinstance(destination, WindowsHeldDirectory):
            raise OSError(errno.ESTALE, "destination handle is unavailable")
        destination.check()
        if source.identity.device != destination.identity.device:
            raise OSError(errno.EXDEV, "cross-volume operation")
        if destination.filesystem is not self:
            raise OSError(errno.ESTALE, "destination belongs to another root")
        try:
            handle = _open_relative(
                _native(destination.descriptor),
                destination_leaf,
                FILE_READ_ATTRIBUTES,
                FILE_NON_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT,
                FILE_OPEN,
            )
        except OSError as error:
            if error.errno in {2, 3, errno.ENOENT}:
                return destination
            raise
        else:
            assert CloseHandle is not None
            CloseHandle(c_void_p(handle))
            if not replace:
                raise OSError(errno.EEXIST, "destination exists")
            return destination

    def rename(
        self,
        source: HeldFile,
        destination_parent: HeldDirectory,
        destination_leaf: str,
        *,
        replace: bool = False,
    ) -> HeldResult[None]:
        try:
            checked = self._check_file(source)
            destination = self._destination(
                checked,
                destination_parent,
                destination_leaf,
                replace=replace,
            )
            _set_name_information(
                checked.descriptor,
                _native(destination.descriptor),
                destination_leaf,
                # The legacy class refuses replacement while the caller retains
                # the exact old target. POSIX semantics keeps that handle valid.
                FILE_RENAME_INFORMATION_EX if replace else FILE_RENAME_INFORMATION,
                replace=replace,
            )
            checked.named = False
            return HeldResult(value=None)
        except OSError as error:
            return HeldResult(error=_error(error))

    def rename_directory(
        self,
        source: HeldDirectory,
        destination_parent: HeldDirectory,
        destination_leaf: str,
    ) -> HeldResult[None]:
        try:
            checked = self._check_directory(source)
            if checked.access != "mutate" or not _leaf(destination_leaf):
                raise OSError(errno.ELOOP, "invalid held directory mutation")
            checked.check_name()
            destination = self._check_directory(destination_parent)
            if checked.identity.device != destination.identity.device:
                raise OSError(errno.EXDEV, "cross-volume operation")
            try:
                existing = _open_relative(
                    _native(destination.descriptor),
                    destination_leaf,
                    FILE_READ_ATTRIBUTES,
                    FILE_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT,
                    FILE_OPEN,
                )
            except OSError as error:
                if error.errno not in {2, 3, errno.ENOENT}:
                    raise
            else:
                assert CloseHandle is not None
                CloseHandle(c_void_p(existing))
                raise OSError(errno.EEXIST, "destination exists")
            _set_name_information(
                checked.descriptor,
                _native(destination.descriptor),
                destination_leaf,
                FILE_RENAME_INFORMATION,
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
            destination = self._destination(
                checked,
                destination_parent,
                destination_leaf,
                replace=False,
            )
            _set_name_information(
                checked.descriptor,
                _native(destination.descriptor),
                destination_leaf,
                FILE_LINK_INFORMATION,
            )
            checked.identity = _identity(_native(checked.descriptor))
            return HeldResult(value=None)
        except OSError as error:
            return HeldResult(error=_error(error))

    def unlink(self, file: HeldFile) -> HeldResult[None]:
        try:
            checked = self._check_file(file)
            if checked.access != "mutate":
                return HeldResult(error=HeldFsError("INVALID_ACCESS", "held file is not mutable"))
            _delete_descriptor(checked.descriptor)
            checked.named = False
            return HeldResult(value=None)
        except OSError as error:
            return HeldResult(error=_error(error))

    def unlink_directory(self, directory: HeldDirectory) -> HeldResult[None]:
        try:
            checked = self._check_directory(directory)
            if checked.access != "mutate":
                raise OSError(errno.ELOOP, "invalid held directory mutation")
            checked.check_name()
            _delete_descriptor(checked.descriptor)
            checked.named = False
            return HeldResult(value=None)
        except OSError as error:
            return HeldResult(error=_error(error))

    def copy(
        self, source: HeldFile, destination_parent: HeldDirectory, destination_leaf: str
    ) -> HeldResult[None]:
        temporary = f".exomem-held-{secrets.token_hex(16)}"
        temporary_file: WindowsHeldFile | None = None
        temporary_identity: StableIdentity | None = None
        destination: WindowsHeldDirectory | None = None
        destination_filesystem: WindowsHeldFilesystem | None = None
        try:
            checked = self._check_file(source)
            if not _leaf(destination_leaf):
                raise OSError(errno.ELOOP, "invalid destination leaf")
            if not isinstance(destination_parent, WindowsHeldDirectory):
                raise OSError(errno.ESTALE, "destination handle is unavailable")
            destination_parent.check()
            if destination_parent.filesystem.closed:
                raise OSError(errno.ESTALE, "destination root is unavailable")
            destination = destination_parent
            destination_filesystem = destination.filesystem
            try:
                existing = destination_filesystem.file(destination, destination_leaf)
                if existing.ok:
                    existing.require().close()
                    raise OSError(errno.EEXIST, "destination exists")
                if existing.error is not None and existing.error.code != "MISSING":
                    raise OSError("destination could not be classified")
            except HeldFsError as error:
                raise OSError(str(error)) from error

            created = destination_filesystem.file(
                destination,
                temporary,
                access="write",
                create=True,
                exclusive=True,
            )
            if not created.ok:
                raise OSError("destination temporary could not be acquired")
            temporary_file = created.require()
            temporary_identity = temporary_file.identity
            os.lseek(checked.descriptor, 0, os.SEEK_SET)
            while chunk := os.read(checked.descriptor, 65536):
                view = memoryview(chunk)
                written = 0
                while written < len(view):
                    written += os.write(temporary_file.descriptor, view[written:])
            os.fsync(temporary_file.descriptor)

            # Reopen the exact temporary with DELETE authority, then rename by handle.
            temporary_file.close()
            temporary_file = None
            mutable = destination_filesystem.file(destination, temporary, access="mutate")
            if not mutable.ok:
                raise OSError("destination temporary mutation handle was refused")
            temporary_file = mutable.require()
            if temporary_file.identity != temporary_identity:
                raise OSError(errno.ESTALE, "destination temporary changed")
            _set_name_information(
                temporary_file.descriptor,
                _native(destination.descriptor),
                destination_leaf,
                FILE_RENAME_INFORMATION,
            )
            temporary_file.named = False
            temporary_file.close()
            temporary_file = None
            return HeldResult(value=None)
        except OSError as error:
            if temporary_file is not None:
                try:
                    if temporary_file.access == "mutate":
                        _delete_descriptor(temporary_file.descriptor)
                except OSError:
                    pass
                temporary_file.close()
            if destination is not None and destination_filesystem is not None:
                cleanup = destination_filesystem.file(destination, temporary, access="mutate")
                if cleanup.ok:
                    with cleanup.require() as file:
                        if temporary_identity is not None and file.identity == temporary_identity:
                            try:
                                _delete_descriptor(file.descriptor)
                            except OSError:
                                pass
            return HeldResult(error=_error(error))

    def enumerate(self, parent: HeldDirectory) -> HeldResult[tuple[SagaRecord, ...]]:
        try:
            checked = self._check_directory(parent)
            records: list[SagaRecord] = []
            self._enumerate(checked, "", records)
            return HeldResult(value=tuple(records))
        except OSError as error:
            return HeldResult(error=_error(error))

    def children(self, parent: HeldDirectory) -> HeldResult[tuple[SagaRecord, ...]]:
        try:
            checked = self._check_directory(parent)
            records: list[SagaRecord] = []
            for name in self._entries(checked):
                try:
                    handle = _open_relative(
                        _native(checked.descriptor),
                        name,
                        FILE_LIST_DIRECTORY | FILE_READ_DATA | FILE_READ_ATTRIBUTES,
                        FILE_SYNCHRONOUS_IO_NONALERT,
                        FILE_OPEN,
                    )
                except OSError as error:
                    if error.errno == errno.ELOOP:
                        continue
                    raise
                descriptor = _fd(handle)
                try:
                    records.append(SagaRecord(name, _identity(_native(descriptor))))
                finally:
                    os.close(descriptor)
            return HeldResult(value=tuple(records))
        except OSError as error:
            return HeldResult(error=_error(error))

    def _entries(self, parent: WindowsHeldDirectory) -> list[str]:
        assert NtQueryDirectoryFile is not None
        buffer = create_string_buffer(65536)
        status = IO_STATUS_BLOCK()
        restart = True
        names: list[str] = []
        while True:
            result = NtQueryDirectoryFile(
                _native(parent.descriptor),
                None,
                None,
                None,
                byref(status),
                buffer,
                len(buffer),
                FILE_BOTH_DIRECTORY_INFORMATION,
                False,
                None,
                restart,
            )
            restart = False
            if (int(result) & 0xFFFFFFFF) == STATUS_NO_MORE_FILES:
                return sorted(names)
            if not _nt_success(result):
                _raise_nt(result, "directory enumeration was refused")
            offset = 0
            while True:
                next_offset = int.from_bytes(buffer.raw[offset : offset + 4], "little")
                name_length = int.from_bytes(buffer.raw[offset + 60 : offset + 64], "little")
                name = buffer.raw[offset + 94 : offset + 94 + name_length].decode("utf-16-le")
                if name not in {".", ".."}:
                    names.append(name)
                if next_offset == 0:
                    break
                offset += next_offset

    def _enumerate(
        self, parent: WindowsHeldDirectory, prefix: str, records: list[SagaRecord]
    ) -> None:
        for name in self._entries(parent):
            handle = _open_relative(
                _native(parent.descriptor),
                name,
                FILE_LIST_DIRECTORY | FILE_READ_DATA | FILE_READ_ATTRIBUTES,
                FILE_SYNCHRONOUS_IO_NONALERT,
                FILE_OPEN,
            )
            descriptor = _fd(handle)
            identity = _identity(_native(descriptor))
            relative = f"{prefix}/{name}" if prefix else name
            records.append(SagaRecord(relative, identity))
            if identity.kind == "directory":
                with WindowsHeldDirectory(self, descriptor) as child:
                    self._enumerate(child, relative, records)
            else:
                os.close(descriptor)


def _probe(root: Path) -> Capabilities:
    if os.name != "nt" or any(
        primitive is None
        for primitive in (
            NtCreateFile,
            NtSetInformationFile,
            SetFileInformationByHandle,
            GetFileInformationByHandleEx,
        )
    ):
        return Capabilities.disabled("native Windows handle primitives are unavailable")
    descriptor = _open_root(root)
    if descriptor is None:
        return Capabilities.disabled("root cannot be opened with stable identity")

    provisional = Capabilities(True, True, True, True, True)
    filesystem = WindowsHeldFilesystem(descriptor, provisional)
    token = secrets.token_hex(16)
    source_name = f".exomem-held-probe-{token}"
    renamed_name = f"{source_name}-renamed"
    linked_name = f"{source_name}-link"
    replacement_source_name = f"{source_name}-replacement-source"
    replacement_target_name = f"{source_name}-replacement-target"
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

            os.symlink(root / source_name, root / alias_name)
            alias = filesystem.file(parent, alias_name)
            if alias.ok:
                alias.require().close()
                return Capabilities.disabled("final reparse refusal is unavailable")
            (root / directory_name).mkdir()
            os.symlink(
                root / directory_name,
                root / directory_alias_name,
                target_is_directory=True,
            )
            alias_parent = filesystem.parent(directory_alias_name)
            if alias_parent.ok:
                alias_parent.require().close()
                return Capabilities.disabled("parent reparse refusal is unavailable")

            with filesystem.file(parent, source_name, access="mutate").require() as source:
                hard_link = filesystem.link(source, parent, linked_name).ok
                renamed = filesystem.rename(source, parent, renamed_name)
                if not renamed.ok:
                    return Capabilities.disabled("same-volume relative rename is unavailable")
            with filesystem.file(parent, renamed_name, access="mutate").require() as renamed:
                if not filesystem.unlink(renamed).ok:
                    return Capabilities.disabled("relative unlink is unavailable")

            for name, data in (
                (replacement_source_name, b"replacement"),
                (replacement_target_name, b"prior"),
            ):
                with filesystem.file(
                    parent,
                    name,
                    access="write",
                    create=True,
                    exclusive=True,
                ).require() as file:
                    if not filesystem.write(file, data).ok:
                        return Capabilities.disabled("relative replacement setup is unavailable")
            with filesystem.file(parent, replacement_target_name).require() as prior:
                with filesystem.file(
                    parent,
                    replacement_source_name,
                    access="mutate",
                ).require() as replacement:
                    if not filesystem.rename(
                        replacement,
                        parent,
                        replacement_target_name,
                        replace=True,
                    ).ok:
                        return Capabilities.disabled("relative replacement is unavailable")
                os.lseek(prior.descriptor, 0, os.SEEK_SET)
                if os.read(prior.descriptor, 5) != b"prior":
                    return Capabilities.disabled("open-target replacement is unavailable")
            with filesystem.file(parent, replacement_target_name).require() as installed:
                if filesystem.read(installed).value != b"replacement":
                    return Capabilities.disabled("relative replacement identity is unavailable")
        return Capabilities(True, True, True, True, hard_link)
    except (HeldFsError, OSError):
        return Capabilities.disabled("actual-filesystem capability probe failed")
    finally:
        filesystem.close()
        for name in (
            source_name,
            renamed_name,
            linked_name,
            replacement_source_name,
            replacement_target_name,
            alias_name,
            directory_alias_name,
        ):
            try:
                (root / name).unlink(missing_ok=True)
            except OSError:
                pass
        try:
            (root / directory_name).rmdir()
        except OSError:
            pass


def probe(root: Path) -> Capabilities:
    return _probe(root)


def acquire(
    root: Path, *, capability: Capabilities | None = None
) -> HeldResult[HeldFilesystem]:
    capability = capability or probe(root)
    if not capability.relative_operations:
        return HeldResult(
            error=HeldFsError(
                "CAPABILITY_UNAVAILABLE", "required filesystem primitives are unavailable"
            )
        )
    descriptor = _open_root(root)
    if descriptor is None:
        return HeldResult(error=HeldFsError("IO_REFUSED", "root anchor could not be acquired"))
    return HeldResult(value=WindowsHeldFilesystem(descriptor, capability))
