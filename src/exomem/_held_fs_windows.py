"""Native Windows held-handle implementation.

Descendants are always opened with ``NtCreateFile`` relative to a retained
directory handle.  This deliberately has no ``CreateFileW(child_path)``
fallback: an unavailable native primitive disables the route.
"""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_byte,
    c_long,
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
    HeldFilesystem,
    HeldFsError,
    HeldResult,
    SagaRecord,
    StableIdentity,
)

FILE_OPEN = 1
FILE_OPEN_IF = 3
FILE_DIRECTORY_FILE = 0x00000001
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
FILE_OPEN_REPARSE_POINT = 0x00200000
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_SHARE_READ = 0x00000001
FILE_LIST_DIRECTORY = 0x00000001
FILE_ADD_FILE = 0x00000002
FILE_ADD_SUBDIRECTORY = 0x00000004
FILE_READ_DATA = 0x00000001
FILE_WRITE_DATA = 0x00000002
FILE_READ_ATTRIBUTES = 0x00000080
DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
FILE_RENAME_INFORMATION = 10
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


if os.name == "nt":  # pragma: no cover - executed by windows-latest
    NtCreateFile = ctypes.windll.ntdll.NtCreateFile
    NtSetInformationFile = ctypes.windll.ntdll.NtSetInformationFile
    NtQueryDirectoryFile = ctypes.windll.ntdll.NtQueryDirectoryFile
    SetFileInformationByHandle = ctypes.windll.kernel32.SetFileInformationByHandle
    GetFileInformationByHandleEx = ctypes.windll.kernel32.GetFileInformationByHandleEx
    GetFileInformationByHandle = ctypes.windll.kernel32.GetFileInformationByHandle
    CreateFileW = ctypes.windll.kernel32.CreateFileW
    CloseHandle = ctypes.windll.kernel32.CloseHandle
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
        c_void_p, POINTER(IO_STATUS_BLOCK), c_void_p, c_ulong, c_ulong
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
else:  # pragma: no cover - keeps Linux imports syntactically safe
    NtCreateFile = None
    NtSetInformationFile = None
    NtQueryDirectoryFile = None
    SetFileInformationByHandle = None
    GetFileInformationByHandleEx = None
    GetFileInformationByHandle = None
    CreateFileW = None
    CloseHandle = None


def _invalid() -> HeldFsError:
    return HeldFsError("UNSAFE_PATH", "unsafe filesystem object")


def _error(_error: OSError | None = None) -> HeldFsError:
    if _error is not None and _error.errno == errno.EXDEV:
        return HeldFsError("CROSS_DEVICE", "operation requires one filesystem")
    if _error is not None and _error.errno in {errno.ELOOP, errno.ENOTDIR}:
        return _invalid()
    return HeldFsError("IO_REFUSED", "held filesystem operation was refused")


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
    return isinstance(name, str) and name not in {"", ".", ".."} and all(
        marker not in name for marker in ("/", "\\", ":", "\x00")
    )


def _native(fd: int) -> int:
    import msvcrt

    return int(msvcrt.get_osfhandle(fd))


def _fd(handle: int) -> int:
    import msvcrt

    return msvcrt.open_osfhandle(handle, os.O_BINARY)


def _nt_success(status: int) -> bool:
    return status >= 0


def _identity(handle: int) -> StableIdentity:
    assert GetFileInformationByHandleEx is not None
    assert GetFileInformationByHandle is not None
    file_id = FILE_ID_INFO()
    attributes = FILE_ATTRIBUTE_TAG_INFO()
    basic = BY_HANDLE_FILE_INFORMATION()
    if not GetFileInformationByHandleEx(handle, FILE_ID_INFO_CLASS, byref(file_id), sizeof(file_id)):
        raise OSError("FileIdInfo is unavailable")
    if not GetFileInformationByHandleEx(
        handle, FILE_ATTRIBUTE_TAG_INFO_CLASS, byref(attributes), sizeof(attributes)
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


def _unicode(name: str) -> tuple[UNICODE_STRING, object]:
    buffer = create_unicode_buffer(name)
    value = UNICODE_STRING(len(name.encode("utf-16-le")), (len(name) + 1) * 2, buffer)
    return value, buffer


def _open_relative(parent_handle: int, name: str, access: int, options: int, disposition: int) -> int:
    """Open exactly one component under ``parent_handle`` through NtCreateFile."""
    assert NtCreateFile is not None
    string, _buffer = _unicode(name)
    attributes = OBJECT_ATTRIBUTES(
        sizeof(OBJECT_ATTRIBUTES),
        c_void_p(parent_handle),
        byref(string),
        0,
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
        FILE_SHARE_READ,
        disposition,
        options | FILE_OPEN_REPARSE_POINT,
        None,
        0,
    )
    if not _nt_success(result) or handle.value in {None, INVALID_HANDLE_VALUE}:
        raise OSError("NtCreateFile refused relative handle operation")
    try:
        _identity(int(handle.value))
        return int(handle.value)
    except OSError:
        assert CloseHandle is not None
        CloseHandle(handle)
        raise


def _probe(root: Path) -> Capabilities:
    if os.name != "nt" or any(
        primitive is None
        for primitive in (NtCreateFile, NtSetInformationFile, SetFileInformationByHandle, GetFileInformationByHandleEx)
    ):
        return Capabilities.disabled("native Windows handle primitives are unavailable")
    descriptor = _open_root(root)
    if descriptor is None:
        return Capabilities.disabled("root cannot be opened with stable identity")
    filesystem = WindowsHeldFilesystem(descriptor)
    source_name = f".exomem-held-probe-{secrets.token_hex(16)}"
    linked_name = f"{source_name}-link"
    renamed_name = f"{source_name}-renamed"
    try:
        with filesystem.parent(".").require() as parent:
            if not filesystem.write(parent, source_name, b"probe").ok:
                return Capabilities.disabled("relative create/write is unavailable")
            source_identity = filesystem.identity(parent, source_name)
            if not source_identity.ok or source_identity.require().kind != "file":
                return Capabilities.disabled("stable final identity is unavailable")
            if not filesystem.link(parent, source_name, parent, linked_name).ok:
                return Capabilities.disabled("relative hard link is unavailable")
            if not filesystem.rename(parent, linked_name, parent, renamed_name).ok:
                return Capabilities.disabled("same-volume relative rename is unavailable")
            if not filesystem.unlink(parent, renamed_name).ok:
                return Capabilities.disabled("relative unlink is unavailable")
            if not filesystem.unlink(parent, source_name).ok:
                return Capabilities.disabled("relative cleanup is unavailable")
    finally:
        filesystem.close()
    return Capabilities(True, True, True, True, True)


def probe(root: Path) -> Capabilities:
    return _probe(root)


def _open_root(root: Path) -> int | None:
    assert CreateFileW is not None
    handle = CreateFileW(
        str(root),
        FILE_LIST_DIRECTORY | FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY | FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ,
        None,
        3,
        0x02000000 | FILE_OPEN_REPARSE_POINT,
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
        CloseHandle(handle)
        return None


class WindowsHeldDirectory(HeldDirectory):
    def __init__(self, filesystem: WindowsHeldFilesystem, descriptor: int) -> None:
        self.filesystem = filesystem
        self.descriptor = descriptor
        self.identity = _identity(_native(descriptor))
        self.closed = False

    def check(self) -> None:
        if self.closed or _identity(_native(self.descriptor)) != self.identity:
            raise OSError("held directory changed")

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            self.closed = True


class WindowsHeldFilesystem(HeldFilesystem):
    def __init__(self, descriptor: int) -> None:
        self.descriptor = descriptor
        self.root_identity = _identity(_native(descriptor))
        self.closed = False

    def close(self) -> None:
        if not self.closed:
            os.close(self.descriptor)
            self.closed = True

    def _check(self, parent: HeldDirectory) -> WindowsHeldDirectory:
        if self.closed or not isinstance(parent, WindowsHeldDirectory) or parent.filesystem is not self:
            raise OSError("held directory is unavailable")
        parent.check()
        return parent

    def parent(self, relative: str, *, create: bool = False) -> HeldResult[HeldDirectory]:
        parts = _parts(relative)
        if parts is None:
            return HeldResult(error=_invalid())
        descriptor = os.dup(self.descriptor)
        try:
            for part in parts:
                handle = _open_relative(
                    _native(descriptor),
                    part,
                    FILE_LIST_DIRECTORY | FILE_ADD_FILE | FILE_ADD_SUBDIRECTORY | FILE_READ_ATTRIBUTES,
                    FILE_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT,
                    FILE_OPEN_IF if create else FILE_OPEN,
                )
                os.close(descriptor)
                descriptor = _fd(handle)
            return HeldResult(value=WindowsHeldDirectory(self, descriptor))
        except OSError as error:
            os.close(descriptor)
            return HeldResult(error=_error(error))

    def _open_leaf(self, parent: HeldDirectory, leaf: str, write: bool = False, create: bool = False, delete: bool = False) -> int:
        if not _leaf(leaf):
            raise OSError(errno.ELOOP, "invalid relative leaf")
        checked = self._check(parent)
        access = FILE_READ_DATA | FILE_READ_ATTRIBUTES
        if write:
            access |= FILE_WRITE_DATA
        if delete:
            access |= DELETE
        handle = _open_relative(
            _native(checked.descriptor),
            leaf,
            access,
            FILE_NON_DIRECTORY_FILE | FILE_SYNCHRONOUS_IO_NONALERT,
            FILE_OPEN_IF if create else FILE_OPEN,
        )
        return _fd(handle)

    def identity(self, parent: HeldDirectory, leaf: str) -> HeldResult[StableIdentity]:
        try:
            descriptor = self._open_leaf(parent, leaf)
            try:
                return HeldResult(value=_identity(_native(descriptor)))
            finally:
                os.close(descriptor)
        except OSError as error:
            return HeldResult(error=_error(error))

    def read(self, parent: HeldDirectory, leaf: str) -> HeldResult[bytes]:
        try:
            descriptor = self._open_leaf(parent, leaf)
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
            descriptor = self._open_leaf(parent, leaf, write=True, create=True)
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.ftruncate(descriptor, 0)
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

    def _paired(self, source: HeldDirectory, destination: HeldDirectory, source_leaf: str, destination_leaf: str) -> tuple[WindowsHeldDirectory, WindowsHeldDirectory]:
        if not _leaf(source_leaf) or not _leaf(destination_leaf):
            raise OSError(errno.ELOOP, "invalid relative leaf")
        source_parent = self._check(source)
        if not isinstance(destination, WindowsHeldDirectory):
            raise OSError("destination handle is unavailable")
        destination.check()
        if source_parent.identity.device != destination.identity.device:
            raise OSError(errno.EXDEV, "cross-volume operation")
        return source_parent, destination

    def _rename_handle(self, descriptor: int, destination: WindowsHeldDirectory, leaf: str) -> None:
        assert SetFileInformationByHandle is not None
        encoded = leaf.encode("utf-16-le")
        payload = create_string_buffer(20 + len(encoded))
        payload[0] = 1
        c_void_p.from_buffer(payload, 8).value = _native(destination.descriptor)
        c_ulong.from_buffer(payload, 16).value = len(encoded)
        payload[20 : 20 + len(encoded)] = encoded
        if not SetFileInformationByHandle(_native(descriptor), FILE_RENAME_INFORMATION, payload, len(payload)):
            raise OSError("relative rename was refused")

    def rename(self, source_parent: HeldDirectory, source_leaf: str, destination_parent: HeldDirectory, destination_leaf: str) -> HeldResult[None]:
        try:
            source, destination = self._paired(source_parent, destination_parent, source_leaf, destination_leaf)
            descriptor = self._open_leaf(source, source_leaf, delete=True)
            try:
                self._rename_handle(descriptor, destination, destination_leaf)
            finally:
                os.close(descriptor)
            return HeldResult(value=None)  # type: ignore[arg-type]
        except OSError as error:
            return HeldResult(error=_error(error))

    def link(self, source_parent: HeldDirectory, source_leaf: str, destination_parent: HeldDirectory, destination_leaf: str) -> HeldResult[None]:
        try:
            source, destination = self._paired(source_parent, destination_parent, source_leaf, destination_leaf)
            descriptor = self._open_leaf(source, source_leaf, delete=True)
            try:
                assert NtSetInformationFile is not None
                encoded = destination_leaf.encode("utf-16-le")
                payload = create_string_buffer(20 + len(encoded))
                payload[0] = 0
                c_void_p.from_buffer(payload, 8).value = _native(destination.descriptor)
                c_ulong.from_buffer(payload, 16).value = len(encoded)
                payload[20 : 20 + len(encoded)] = encoded
                status = IO_STATUS_BLOCK()
                if not _nt_success(NtSetInformationFile(_native(descriptor), byref(status), payload, len(payload), FILE_LINK_INFORMATION)):
                    raise OSError("relative hard link was refused")
            finally:
                os.close(descriptor)
            return HeldResult(value=None)  # type: ignore[arg-type]
        except OSError as error:
            return HeldResult(error=_error(error))

    def unlink(self, parent: HeldDirectory, leaf: str) -> HeldResult[None]:
        try:
            descriptor = self._open_leaf(parent, leaf, delete=True)
            try:
                assert SetFileInformationByHandle is not None
                delete = wintypes.BOOL(True)
                if not SetFileInformationByHandle(_native(descriptor), FILE_DISPOSITION_INFORMATION, byref(delete), sizeof(delete)):
                    raise OSError("relative disposition was refused")
            finally:
                os.close(descriptor)
            return HeldResult(value=None)  # type: ignore[arg-type]
        except OSError as error:
            return HeldResult(error=_error(error))

    def copy(self, source_parent: HeldDirectory, source_leaf: str, destination_parent: HeldDirectory, destination_leaf: str) -> HeldResult[None]:
        temporary = f".exomem-held-{secrets.token_hex(16)}"
        try:
            if not _leaf(source_leaf) or not _leaf(destination_leaf):
                raise OSError(errno.ELOOP, "invalid relative leaf")
            source = self._check(source_parent)
            if not isinstance(destination_parent, WindowsHeldDirectory):
                raise OSError("destination handle is unavailable")
            destination_parent.check()
            destination = destination_parent
            source_descriptor = self._open_leaf(source, source_leaf)
            temporary_descriptor = self._open_leaf(destination, temporary, write=True, create=True)
            try:
                while chunk := os.read(source_descriptor, 65536):
                    view = memoryview(chunk)
                    written = 0
                    while written < len(view):
                        written += os.write(temporary_descriptor, view[written:])
                os.fsync(temporary_descriptor)
                self._rename_handle(temporary_descriptor, destination, destination_leaf)
            finally:
                os.close(source_descriptor)
                os.close(temporary_descriptor)
            return HeldResult(value=None)  # type: ignore[arg-type]
        except OSError as error:
            return HeldResult(error=_error(error))

    def enumerate(self, parent: HeldDirectory) -> HeldResult[tuple[SagaRecord, ...]]:
        """Enumerate via ``NtQueryDirectoryFile``; recursive records are name ordered."""
        try:
            checked = self._check(parent)
            records: list[SagaRecord] = []
            self._enumerate(checked, "", records)
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
            result = NtQueryDirectoryFile(_native(parent.descriptor), None, None, None, byref(status), buffer, len(buffer), FILE_BOTH_DIRECTORY_INFORMATION, False, None, restart)
            restart = False
            if (int(result) & 0xFFFFFFFF) == STATUS_NO_MORE_FILES:
                return sorted(names)
            if not _nt_success(result):
                raise OSError("directory enumeration was refused")
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

    def _enumerate(self, parent: WindowsHeldDirectory, prefix: str, records: list[SagaRecord]) -> None:
        for name in self._entries(parent):
            handle = _open_relative(_native(parent.descriptor), name, FILE_READ_ATTRIBUTES, FILE_SYNCHRONOUS_IO_NONALERT, FILE_OPEN)
            descriptor = _fd(handle)
            try:
                identity = _identity(_native(descriptor))
                relative = f"{prefix}/{name}" if prefix else name
                records.append(SagaRecord(relative, identity))
                if identity.kind == "directory":
                    self._enumerate(WindowsHeldDirectory(self, descriptor), relative, records)
                    descriptor = -1
            finally:
                if descriptor != -1:
                    os.close(descriptor)


def acquire(root: Path) -> HeldResult[HeldFilesystem]:
    capability = probe(root)
    if not capability.relative_operations:
        return HeldResult(error=HeldFsError("CAPABILITY_UNAVAILABLE", "required filesystem primitives are unavailable"))
    descriptor = _open_root(root)
    if descriptor is None:
        return HeldResult(error=HeldFsError("IO_REFUSED", "root anchor could not be acquired"))
    return HeldResult(value=WindowsHeldFilesystem(descriptor))
