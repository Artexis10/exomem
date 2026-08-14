"""Process-safe, re-entrant serialization for one Exomem vault.

The lock file is runtime coordination state, not authority or durable vault
content.  A process-local ``RLock`` handles threads and nested command helpers;
an OS lock on the stable file handles separate processes.  The operating system
releases the latter when a process exits, so a leftover file is harmless.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import math
import os
import re
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, TypedDict, cast

from .cli_ops import OpError

if os.name == "nt":
    import msvcrt
else:
    import fcntl


_DEFAULT_TIMEOUT_SECONDS = 5.0
_DEFAULT_POLL_INTERVAL_SECONDS = 0.025
_BUSY_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EDEADLK})
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_DEFAULT_LONG_HOLDER_SECONDS = 30.0
_STATUS_TIMEOUT_SECONDS = 0.25
_HOLDER_SCHEMA = 1

# The most recent `wait_ms`/`hold_ms` this task/thread measured acquiring and
# holding the vault mutation boundary — set by `VaultMutationCoordinator.hold()`
# for the OUTER (non-reentrant) acquisition only, so a nested command helper
# reusing an already-held lock never overwrites its caller's timing. Read by
# `writer_lease.invoke()` at the mutation-journal seam (O5) and by the R1
# telemetry events, both of which run in the same execution context shortly
# after the `with ...hold():` block exits.
class MutationTiming(TypedDict):
    """Completed outer mutation-boundary timing and identifying metadata."""

    wait_ms: float
    hold_ms: float
    operation: str
    holder_kind: str


_LAST_MUTATION_TIMING: ContextVar[MutationTiming | None] = ContextVar(
    "exomem_last_mutation_timing", default=None
)


def last_mutation_timing() -> MutationTiming | None:
    """Return the most recent completed boundary hold in this context, or `None`."""
    return _LAST_MUTATION_TIMING.get()


logger = logging.getLogger(__name__)


def _windows_library(ctypes_module: Any, name: str) -> Any:
    """Keep Windows-only ctypes attributes out of non-Windows type stubs."""
    return getattr(ctypes_module, "WinDLL")(name, use_last_error=True)


def _windows_last_error(ctypes_module: Any) -> int:
    return int(getattr(ctypes_module, "get_last_error")())


def _log_mutation_lock_event(event: str, **fields: Any) -> None:
    try:
        from .log_events import log_event

        log_event(logger, logging.INFO, event, fields=fields)
    except Exception:  # noqa: BLE001 - observability must never break a mutation
        pass


def _bump_boundary_metric(name: str, labels: dict[str, str] | None = None) -> None:
    try:
        from . import metrics

        metrics.inc_counter(name, labels or {})
    except Exception:  # noqa: BLE001 - observability must never break a mutation
        pass


def _observe_boundary_ms(name: str, value_ms: float) -> None:
    try:
        from . import metrics

        metrics.observe_duration_ms(name, value_ms, {})
    except Exception:  # noqa: BLE001 - observability must never break a mutation
        pass


def canonical_mutation_identity(vault_or_cell: os.PathLike[str] | str) -> str:
    """Return a stable, non-display identity for a vault path or opaque cell ID."""
    if isinstance(vault_or_cell, os.PathLike):
        resolved = Path(vault_or_cell).expanduser().resolve(strict=False)
        return f"vault:{os.path.normcase(str(resolved))}"
    value = str(vault_or_cell).strip()
    if not value:
        raise ValueError("mutation identity must not be empty")
    return f"cell:{value}"


def nofollow_regular_file_identity(path: Path) -> tuple[int, int, int, int, int]:
    """Return one regular entry identity, rejecting links and Windows reparse points."""
    info = os.lstat(path)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse_point:
        raise OSError("path is not a regular no-follow file")
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_mode),
        int(info.st_size),
        int(info.st_mtime_ns),
    )


@dataclass
class RetainedRegularFile:
    """One no-follow regular file pinned until an atomic same-volume rename."""

    path: Path
    directory: _SecureDirectory
    fd: int
    identity: tuple[int, ...]

    def close(self) -> None:
        try:
            os.close(self.fd)
        finally:
            self.directory.close()


def retain_regular_file(path: Path) -> RetainedRegularFile:
    """Pin one regular child without following aliases or replacement races."""
    target = Path(path)
    if target.name != target.as_posix().split("/")[-1]:
        raise OSError("retained file target must be one child basename")
    directory = _acquire_secure_directory(target.parent, create=False)
    try:
        fd = _open_secure_file_at(
            directory, target.name, os.O_RDONLY, delete_access=True
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not _same_file_entry(directory, target.name, fd):
                raise OSError("regular file changed while being retained")
            if os.name == "nt":
                import msvcrt

                identity: tuple[int, ...] = _windows_handle_identity(msvcrt.get_osfhandle(fd))
            else:
                identity = (int(info.st_dev), int(info.st_ino))
            return RetainedRegularFile(target, directory, fd, identity)
        except BaseException:
            os.close(fd)
            raise
    except BaseException:
        directory.close()
        raise


def rename_retained_regular_file(source: RetainedRegularFile, destination: Path) -> None:
    """Rename the exact retained source to a new sibling in a retained directory."""
    target = Path(destination)
    if target.name != target.as_posix().split("/")[-1]:
        raise OSError("retained rename destination must be one child basename")
    directory = _acquire_secure_directory(target.parent, create=False)
    try:
        if not _same_directory_path(source.directory) or not _same_directory_path(directory):
            raise OSError("retained rename directory changed")
        try:
            os.lstat(target)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("retained rename destination exists")
        if os.name != "nt":
            assert source.directory.fd is not None and directory.fd is not None
            if not _same_file_entry(source.directory, source.path.name, source.fd):
                raise OSError("retained rename source identity changed")
            os.rename(
                source.path.name,
                target.name,
                src_dir_fd=source.directory.fd,
                dst_dir_fd=directory.fd,
            )
            destination_info = os.stat(
                target.name, dir_fd=directory.fd, follow_symlinks=False
            )
            source_info = os.fstat(source.fd)
            if (
                not stat.S_ISREG(destination_info.st_mode)
                or destination_info.st_dev != source_info.st_dev
                or destination_info.st_ino != source_info.st_ino
            ):
                raise OSError("retained rename destination identity changed")
            try:
                os.stat(source.path.name, dir_fd=source.directory.fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            raise OSError("retained rename source still exists")
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class _RenameInfo(ctypes.Structure):
            _fields_ = [
                ("replace", wintypes.BOOLEAN),
                ("root", wintypes.HANDLE),
                ("length", wintypes.DWORD),
                ("filename", wintypes.WCHAR * 1),
            ]

        name = target.name.encode("utf-16-le")
        filename_offset = _RenameInfo.filename.offset
        size = filename_offset + len(name)
        raw = ctypes.create_string_buffer(size)
        info = _RenameInfo.from_buffer(raw)
        info.replace = False
        info.root = directory.windows_handle
        info.length = len(name)
        ctypes.memmove(ctypes.addressof(raw) + filename_offset, name, len(name))
        kernel32 = _windows_library(ctypes, "kernel32")
        rename = kernel32.SetFileInformationByHandle
        rename.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        rename.restype = wintypes.BOOL
        if not rename(msvcrt.get_osfhandle(source.fd), 3, raw, size):
            raise OSError(_windows_last_error(ctypes), "retained Windows rename refused")
        destination_handle = _windows_open_path(directory.path / target.name, directory=False)
        try:
            if _windows_handle_identity(destination_handle) != source.identity:
                raise OSError("retained Windows rename destination identity changed")
        finally:
            _windows_close_handle(destination_handle)
        try:
            replacement = _windows_open_path(source.directory.path / source.path.name, directory=False)
        except FileNotFoundError:
            return
        _windows_close_handle(replacement)
        raise OSError("retained Windows rename source still exists")
    finally:
        directory.close()


class _SecureDirectory:
    """A retained non-reparse directory handle used for runtime lock files."""

    def __init__(
        self,
        path: Path,
        *,
        fd: int | None = None,
        windows_handles: list[int] | None = None,
        close_fd: Callable[[int], None] | None = None,
        close_windows_handle: Callable[[int], None] | None = None,
    ) -> None:
        self.path = path
        self.fd = fd
        self.windows_handles = windows_handles or []
        # Retain the primitive rather than looking it up during a late atexit
        # cleanup. A daemon that owns a graph rebuild may outlive normal module
        # teardown ordering.
        self._close_fd = close_fd or os.close
        self._close_windows_handle = close_windows_handle or _windows_close_handle

    @property
    def windows_handle(self) -> int:
        if not self.windows_handles:
            raise OSError("secure Windows directory handle is unavailable")
        return self.windows_handles[-1]

    def close(self) -> None:
        if self.fd is not None:
            self._close_fd(self.fd)
            self.fd = None
        while self.windows_handles:
            self._close_windows_handle(self.windows_handles.pop())


def _windows_open_path(
    path: Path,
    *,
    directory: bool,
    access: int = 0x00020080,  # READ_CONTROL | FILE_READ_ATTRIBUTES
    share: int = 0x3,
    creation: int = 3,
) -> int:
    """Open one exact Windows path entry without following reparse points."""
    import ctypes
    from ctypes import wintypes

    kernel32 = _windows_library(ctypes, "kernel32")
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    flags = 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
    handle = create_file(str(path), access, share, None, creation, flags, None)
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        error = _windows_last_error(ctypes)
        if error in {2, 3}:
            raise FileNotFoundError(error, f"cannot safely open {path.name}")
        if error in {80, 183}:
            raise FileExistsError(error, f"path already exists: {path.name}")
        raise OSError(error, f"cannot safely open {path.name}")

    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]

    info = _AttributeTagInfo()
    try:
        get_info = kernel32.GetFileInformationByHandleEx
        get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        get_info.restype = wintypes.BOOL
        if not get_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(_windows_last_error(ctypes), f"cannot inspect {path.name}")
        if info.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise OSError("reparse points are not allowed")
        if bool(info.attributes & 0x10) != directory:
            raise OSError("unexpected path type")
    except BaseException:
        kernel32.CloseHandle(handle)
        raise
    return int(handle)


def _windows_close_handle(handle: int) -> None:
    import ctypes

    _windows_library(ctypes, "kernel32").CloseHandle(handle)


def _windows_final_path(handle: int) -> str:
    """Return the resolved path of one retained Windows handle."""
    import ctypes
    from ctypes import wintypes

    kernel32 = _windows_library(ctypes, "kernel32")
    get_final_path = kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_final_path.restype = wintypes.DWORD
    needed = get_final_path(handle, None, 0, 0)
    if not needed:
        raise OSError(_windows_last_error(ctypes), "cannot resolve retained Windows path")
    buffer = ctypes.create_unicode_buffer(needed + 1)
    written = get_final_path(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise OSError(_windows_last_error(ctypes), "cannot resolve retained Windows path")
    return str(buffer.value)


def _normalized_windows_path(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return value.rstrip("\\/").replace("/", "\\").casefold()


def _windows_child_is_in_directory(directory: _SecureDirectory, handle: int) -> bool:
    parent = _normalized_windows_path(_windows_final_path(directory.windows_handle))
    child = _normalized_windows_path(_windows_final_path(handle))
    return child.rsplit("\\", 1)[0] == parent


def _windows_current_user_sid() -> str:
    """Return the current process token SID without shelling out to icacls."""
    import ctypes
    from ctypes import wintypes

    token_query = 0x0008
    token_user = 1
    token = wintypes.HANDLE()
    kernel32 = _windows_library(ctypes, "kernel32")
    advapi32 = _windows_library(ctypes, "advapi32")
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    open_process_token.restype = wintypes.BOOL
    if not open_process_token(get_current_process(), token_query, ctypes.byref(token)):
        raise OSError(_windows_last_error(ctypes), "cannot open current Windows token")
    try:
        needed = wintypes.DWORD()
        get_token_information = advapi32.GetTokenInformation
        get_token_information.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        get_token_information.restype = wintypes.BOOL
        get_token_information(token, token_user, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise OSError(_windows_last_error(ctypes), "cannot measure current Windows token SID")
        buffer = ctypes.create_string_buffer(needed.value)
        if not get_token_information(
            token, token_user, buffer, needed, ctypes.byref(needed)
        ):
            raise OSError(_windows_last_error(ctypes), "cannot read current Windows token SID")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        convert = advapi32.ConvertSidToStringSidW
        convert.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
        convert.restype = wintypes.BOOL
        if not convert(sid, ctypes.byref(text)):
            raise OSError(_windows_last_error(ctypes), "cannot render current Windows token SID")
        try:
            return str(text.value)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.CloseHandle(token)


def _windows_private_dacl_trustees(sid: str) -> tuple[str, ...]:
    """Return the distinct SDDL trustees permitted for private runtime state."""
    if not re.fullmatch(r"S-1-[0-9-]+", sid):
        raise ValueError("invalid current Windows SID")
    aliases = {"S-1-5-18": "SY", "S-1-5-32-544": "BA"}
    principals = (aliases.get(sid, sid), "SY", "BA")
    unique: dict[str, str] = {}
    for principal in principals:
        unique.setdefault(principal.casefold(), principal)
    return tuple(unique.values())


class WindowsRuntimeDaclError(RuntimeError):
    """Actionable fail-closed result for one unsafe idempotency runtime entry."""

    def __init__(self, path: Path, remediation: str):
        self.path = path
        self.remediation = remediation
        super().__init__(
            f"unsafe Windows DACL at {path}; run in elevated PowerShell: {remediation}"
        )


def _windows_private_dacl_repair_command(
    path: Path, sid: str, *, directory: bool
) -> str:
    """Render an explicit, non-recursive repair for exactly one runtime entry."""
    trustees = {
        "SY": "S-1-5-18",
        "BA": "S-1-5-32-544",
    }
    trustee_sids = tuple(
        trustees.get(trustee, trustee)
        for trustee in _windows_private_dacl_trustees(sid)
    )
    quoted_path = "'" + str(path).replace("'", "''") + "'"
    flags = "(OI)(CI)" if directory else ""
    grants = " ".join(
        f"'*{trustee}:{flags}F'" for trustee in trustee_sids
    )
    return (
        f"icacls.exe {quoted_path} /reset; if ($LASTEXITCODE -eq 0) {{ "
        f"icacls.exe {quoted_path} /inheritance:r /grant:r {grants} }}"
    )


def _windows_private_dacl_sddl(sid: str) -> str:
    """Protected, inheritable DACL: current user plus only OS recovery principals."""
    return "D:P" + "".join(
        f"(A;OICI;FA;;;{trustee})" for trustee in _windows_private_dacl_trustees(sid)
    )


def _windows_apply_dacl_sddl(path: Path, sddl: str) -> None:
    """Apply one native SDDL DACL without shelling out through a localized tool."""
    import ctypes
    from ctypes import wintypes

    descriptor = wintypes.LPVOID()
    advapi32 = _windows_library(ctypes, "advapi32")
    kernel32 = _windows_library(ctypes, "kernel32")
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), wintypes.LPVOID]
    convert.restype = wintypes.BOOL
    if not convert(sddl, 1, ctypes.byref(descriptor), None):
        raise OSError(_windows_last_error(ctypes), "cannot build Windows runtime DACL")
    try:
        set_security = advapi32.SetFileSecurityW
        set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
        set_security.restype = wintypes.BOOL
        dacl_information = 0x00000004 | 0x80000000
        if not set_security(str(path), dacl_information, descriptor):
            raise OSError(_windows_last_error(ctypes), "cannot protect Windows runtime DACL")
    finally:
        kernel32.LocalFree(descriptor)


def _windows_apply_private_dacl(path: Path, sid: str) -> None:
    """Set a protected inheritable DACL on a newly-created runtime directory."""
    _windows_apply_dacl_sddl(path, _windows_private_dacl_sddl(sid))


def _windows_dacl_sddl(path: Path) -> str:
    """Read only the DACL text through native security APIs."""
    import ctypes
    from ctypes import wintypes

    security_descriptor = wintypes.LPVOID()
    text = wintypes.LPWSTR()
    advapi32 = _windows_library(ctypes, "advapi32")
    kernel32 = _windows_library(ctypes, "kernel32")
    get_security = advapi32.GetNamedSecurityInfoW
    get_security.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID,
        wintypes.LPVOID, wintypes.LPVOID, ctypes.POINTER(wintypes.LPVOID),
    ]
    get_security.restype = wintypes.DWORD
    dacl_information = 0x00000004
    result = get_security(
        str(path), 1, dacl_information, None, None, None, None, ctypes.byref(security_descriptor)
    )
    if result:
        raise OSError(result, "cannot inspect Windows runtime DACL")
    try:
        convert = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
        convert.argtypes = [
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(wintypes.DWORD),
        ]
        convert.restype = wintypes.BOOL
        size = wintypes.DWORD()
        if not convert(security_descriptor, 1, dacl_information, ctypes.byref(text), ctypes.byref(size)):
            raise OSError(_windows_last_error(ctypes), "cannot render Windows runtime DACL")
        try:
            return str(text.value)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(security_descriptor)


def _windows_dacl_sddl_for_handle(handle: int) -> str:
    """Read the DACL bound to a retained Windows handle."""
    import ctypes
    from ctypes import wintypes

    security_descriptor = wintypes.LPVOID()
    text = wintypes.LPWSTR()
    advapi32 = _windows_library(ctypes, "advapi32")
    kernel32 = _windows_library(ctypes, "kernel32")
    get_security = advapi32.GetSecurityInfo
    get_security.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID,
        wintypes.LPVOID, wintypes.LPVOID, ctypes.POINTER(wintypes.LPVOID),
    ]
    get_security.restype = wintypes.DWORD
    dacl_information = 0x00000004
    result = get_security(handle, 1, dacl_information, None, None, None, None, ctypes.byref(security_descriptor))
    if result:
        raise OSError(result, "cannot inspect Windows runtime DACL")
    try:
        convert = advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW
        convert.argtypes = [
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(wintypes.DWORD),
        ]
        convert.restype = wintypes.BOOL
        size = wintypes.DWORD()
        if not convert(security_descriptor, 1, dacl_information, ctypes.byref(text), ctypes.byref(size)):
            raise OSError(_windows_last_error(ctypes), "cannot render Windows runtime DACL")
        try:
            return str(text.value)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(security_descriptor)


def _windows_private_dacl_is_valid(sddl: str, sid: str, *, directory: bool) -> bool:
    """Reject broad or altered ACEs; inherited file ACEs are accepted precisely."""
    if not isinstance(sddl, str) or "D:" not in sddl:
        return False
    dacl = sddl.split("D:", 1)[1]
    if directory and not dacl.startswith("P"):
        return False
    aces = re.findall(r"\(([^()]*)\)", dacl)
    expected = {trustee.casefold() for trustee in _windows_private_dacl_trustees(sid)}
    observed: set[str] = set()
    for ace in aces:
        fields = ace.split(";")
        if len(fields) != 6:
            return False
        kind, flags, rights, _object_guid, _inherit_object_guid, trustee = fields
        if kind != "A" or rights.casefold() not in {"fa", "0x1f01ff"}:
            return False
        if trustee.casefold() not in expected or trustee.casefold() in observed:
            return False
        normalized_flags = flags.casefold()
        allowed = {"", "oici", "idoici", "id"} if not directory else {"oici"}
        if normalized_flags not in allowed:
            return False
        observed.add(trustee.casefold())
    return observed == expected


def _validate_windows_runtime_entry(
    path: Path, *, directory: bool, sid: str, handle: int | None = None
) -> None:
    """Validate one path through a scoped non-reparse handle."""
    opened_here = handle is None
    if handle is None:
        handle = _windows_open_path(path, directory=directory)
    try:
        sddl = _windows_dacl_sddl_for_handle(handle) if not opened_here else _windows_dacl_sddl(path)
        if not _windows_private_dacl_is_valid(sddl, sid, directory=directory):
            raise WindowsRuntimeDaclError(
                path,
                _windows_private_dacl_repair_command(path, sid, directory=directory),
            )
    finally:
        if opened_here:
            _windows_close_handle(handle)


def prepare_windows_idempotency_runtime_paths(state_dir: Path, owners_dir: Path) -> None:
    """Create the two runtime directories with protected DACLs, never repair old ones."""
    if os.name != "nt":
        return
    sid = _windows_current_user_sid()

    def acquire(path: Path) -> tuple[_SecureDirectory, bool]:
        try:
            return _acquire_secure_directory(path, create=False), False
        except FileNotFoundError:
            return _acquire_secure_directory(path, create=True), True

    state, state_created = acquire(state_dir)
    try:
        if state_created:
            _windows_apply_private_dacl(state_dir, sid)
        _validate_windows_runtime_entry(
            state_dir, directory=True, sid=sid, handle=state.windows_handle
        )
        owners, owners_created = acquire(owners_dir)
        try:
            if not _windows_child_is_in_directory(state, owners.windows_handle):
                raise OSError("Windows owner directory escaped its retained state directory")
            if owners_created:
                _windows_apply_private_dacl(owners_dir, sid)
            _validate_windows_runtime_entry(
                owners_dir, directory=True, sid=sid, handle=owners.windows_handle
            )
        finally:
            owners.close()
    finally:
        state.close()


def validate_windows_idempotency_runtime_paths(
    state_dir: Path, owners_dir: Path, private_paths: tuple[Path, ...]
) -> None:
    """Reject reparse points or broadened runtime state before SQLite/pickle reads."""
    if os.name != "nt":
        return
    sid = _windows_current_user_sid()
    with _open_secure_directory(state_dir, create=False) as state:
        _validate_windows_runtime_entry(
            state_dir, directory=True, sid=sid, handle=state.windows_handle
        )
        with _open_secure_directory(owners_dir, create=False) as owners:
            if not _windows_child_is_in_directory(state, owners.windows_handle):
                raise OSError("Windows owner directory escaped its retained state directory")
            _validate_windows_runtime_entry(
                owners_dir, directory=True, sid=sid, handle=owners.windows_handle
            )
        for path in private_paths:
            if path.parent != state_dir:
                raise RuntimeError("idempotency runtime database escaped its trusted state directory")
            try:
                fd = _open_secure_file_at(state, path.name, os.O_RDONLY)
            except FileNotFoundError:
                continue
            try:
                _validate_windows_runtime_entry(
                    path,
                    directory=False,
                    sid=sid,
                    handle=getattr(msvcrt, "get_osfhandle")(fd),  # noqa: B009 - Windows-only stub
                )
            finally:
                os.close(fd)


def _windows_handle_identity(handle: int) -> tuple[int, int, int]:
    import ctypes
    from ctypes import wintypes

    class _FileInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("access_time", wintypes.FILETIME),
            ("write_time", wintypes.FILETIME),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    info = _FileInfo()
    kernel32 = _windows_library(ctypes, "kernel32")
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FileInfo)]
    get_info.restype = wintypes.BOOL
    if not get_info(handle, ctypes.byref(info)):
        raise OSError(_windows_last_error(ctypes), "cannot identify retained Windows handle")
    return info.volume_serial, info.file_index_high, info.file_index_low


def _open_posix_directory(path: Path, *, create: bool, mode: int) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open(path.anchor or "/", flags)
    try:
        for part in path.parts[1:]:
            try:
                child_fd = os.open(part, flags, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(part, mode=mode, dir_fd=current_fd)
                child_fd = os.open(part, flags, dir_fd=current_fd)
            if not stat.S_ISDIR(os.fstat(child_fd).st_mode):
                os.close(child_fd)
                raise OSError("non-directory path component")
            os.close(current_fd)
            current_fd = child_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _acquire_windows_secure_directory(
    absolute: Path,
    *,
    create: bool,
    mode: int,
    open_path: Callable[..., int] = _windows_open_path,
    close_handle: Callable[[int], None] = _windows_close_handle,
) -> _SecureDirectory:
    """Open every directory component without following a Windows reparse point."""
    handles: list[int] = []
    current = Path(absolute.anchor)
    try:
        handles.append(open_path(current, directory=True))
        for part in absolute.parts[1:]:
            current /= part
            try:
                handles.append(open_path(current, directory=True))
            except FileNotFoundError:
                if not create:
                    raise
                current.mkdir(mode=mode)
                handles.append(open_path(current, directory=True))
        return _SecureDirectory(
            absolute, windows_handles=handles, close_windows_handle=close_handle
        )
    except BaseException:
        while handles:
            close_handle(handles.pop())
        raise


def _acquire_secure_directory(
    path: Path, *, create: bool, mode: int = 0o700
) -> _SecureDirectory:
    """Return a retained secure directory handle for explicit owners."""
    absolute = path.expanduser().absolute()
    if os.name != "nt":
        directory = _SecureDirectory(
            absolute,
            fd=_open_posix_directory(absolute, create=create, mode=mode),
        )
    else:
        directory = _acquire_windows_secure_directory(absolute, create=create, mode=mode)
    return directory


def _acquire_trusted_runtime_root(path: Path) -> _SecureDirectory:
    """Pin the configured runtime root after validating its local authority."""
    absolute = path.expanduser().absolute()
    created = False
    try:
        os.lstat(absolute)
    except FileNotFoundError:
        created = True
    directory = _acquire_secure_directory(absolute, create=True, mode=0o700)
    try:
        if os.name != "nt":
            assert directory.fd is not None
            if created:
                os.fchmod(directory.fd, 0o700)
            info = os.fstat(directory.fd)
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o022
            ):
                raise OSError("runtime state root is not owner-controlled")
        return directory
    except BaseException:
        directory.close()
        raise


@contextlib.contextmanager
def _open_secure_directory(
    path: Path, *, create: bool, mode: int = 0o700
) -> Iterator[_SecureDirectory]:
    """Pin the lock directory and, on Windows, every non-reparse ancestor."""
    directory = _acquire_secure_directory(path, create=create, mode=mode)
    try:
        yield directory
    finally:
        directory.close()


def _same_directory_path(directory: _SecureDirectory) -> bool:
    try:
        if os.name != "nt":
            current = os.lstat(directory.path)
            assert directory.fd is not None
            retained = os.fstat(directory.fd)
            return (
                stat.S_ISDIR(current.st_mode)
                and current.st_dev == retained.st_dev
                and current.st_ino == retained.st_ino
            )
        current_handle = _windows_open_path(directory.path, directory=True)
        try:
            return _windows_handle_identity(current_handle) == _windows_handle_identity(
                directory.windows_handle
            )
        finally:
            _windows_close_handle(current_handle)
    except OSError:
        return False


def _same_file_entry(directory: _SecureDirectory, name: str, fd: int) -> bool:
    try:
        if os.name != "nt":
            current = os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
            retained = os.fstat(fd)
            return (
                stat.S_ISREG(current.st_mode)
                and current.st_dev == retained.st_dev
                and current.st_ino == retained.st_ino
            )
        current_handle = _windows_open_path(directory.path / name, directory=False)
        try:
            return _windows_handle_identity(current_handle) == _windows_handle_identity(
                getattr(msvcrt, "get_osfhandle")(fd)
            )
        finally:
            _windows_close_handle(current_handle)
    except OSError:
        return False


def retain_secure_directory(path: Path) -> _SecureDirectory:
    """Pin one existing no-follow directory for bounded child operations."""
    directory = _acquire_secure_directory(Path(path), create=False)
    if not _same_directory_path(directory):
        directory.close()
        raise OSError("retained directory changed")
    return directory


def create_retained_child_directory(parent: _SecureDirectory, name: str) -> _SecureDirectory:
    """Create one child exclusively from a retained parent, then pin it."""
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise OSError("retained directory child must be one basename")
    if not _same_directory_path(parent):
        raise OSError("retained parent changed")
    if os.name != "nt":
        assert parent.fd is not None
        os.mkdir(name, 0o700, dir_fd=parent.fd)
        os.fsync(parent.fd)
        fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent.fd,
        )
        return _SecureDirectory(parent.path / name, fd=fd)
    child_path = parent.path / name
    # Win32 has no exposed handle-relative mkdir primitive here. Pin the parent
    # identity before and after the operation, then pin/prove the new child is
    # immediately below that same parent before returning it.
    child_path.mkdir(mode=0o700)
    if not _same_directory_path(parent):
        raise OSError("retained Windows parent changed during child creation")
    child = _acquire_secure_directory(child_path, create=False)
    try:
        if not _windows_child_is_in_directory(parent, child.windows_handle):
            raise OSError("Windows child directory escaped its retained parent")
        return child
    except BaseException:
        child.close()
        raise


def retain_child_directory(parent: _SecureDirectory, name: str) -> _SecureDirectory:
    """Pin an existing direct child and prove it still belongs to the parent."""
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise OSError("retained directory child must be one basename")
    if not _same_directory_path(parent):
        raise OSError("retained parent changed")
    if os.name != "nt":
        assert parent.fd is not None
        fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent.fd,
        )
        return _SecureDirectory(parent.path / name, fd=fd)
    child = _acquire_secure_directory(parent.path / name, create=False)
    try:
        if not _same_directory_path(parent) or not _windows_child_is_in_directory(parent, child.windows_handle):
            raise OSError("retained Windows child directory changed or escaped parent")
        return child
    except BaseException:
        child.close()
        raise


def retained_write_file(
    directory: _SecureDirectory, name: str, content: bytes, *, replace: bool = False
) -> None:
    """Durably write one bounded regular child through its retained parent."""
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise OSError("retained file child must be one basename")
    if not _same_directory_path(directory):
        raise OSError("retained directory changed")
    if os.name != "nt":
        assert directory.fd is not None
        if not replace:
            try:
                os.stat(name, dir_fd=directory.fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError("retained destination exists")
        staging = f".{name}.new"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(staging, flags, 0o600, dir_fd=directory.fd)
        try:
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        if replace:
            os.rename(staging, name, src_dir_fd=directory.fd, dst_dir_fd=directory.fd)
        else:
            # link() is no-replace: unlike rename(), it cannot overwrite a
            # destination that appeared after the retained-directory census.
            os.link(
                staging,
                name,
                src_dir_fd=directory.fd,
                dst_dir_fd=directory.fd,
                follow_symlinks=False,
            )
            os.unlink(staging, dir_fd=directory.fd)
        os.fsync(directory.fd)
        return
    target = directory.path / name
    staging = directory.path / f".{name}.new"
    if staging.exists():
        raise FileExistsError("retained Windows staging entry exists")
    fd = _open_secure_file_at(directory, staging.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    try:
        os.write(fd, content)
        os.fsync(fd)
    finally:
        os.close(fd)
    if not replace and target.exists():
        raise FileExistsError("retained Windows destination exists")
    os.replace(staging, target)
    if not _same_directory_path(directory):
        raise OSError("retained Windows directory changed during file publish")


def retained_read_file(directory: _SecureDirectory, name: str, *, limit: int) -> bytes:
    """Read one no-follow regular child from a retained directory."""
    if limit <= 0 or not name or Path(name).name != name:
        raise OSError("invalid retained file read")
    fd = _open_secure_file_at(directory, name, os.O_RDONLY)
    try:
        if not _same_file_entry(directory, name, fd):
            raise OSError("retained file changed")
        data = os.read(fd, limit + 1)
        if len(data) > limit:
            raise OSError("retained file exceeds bounded read")
        return data
    finally:
        os.close(fd)


def retained_unlink_file(directory: _SecureDirectory, name: str) -> None:
    """Remove one existing retained regular child; never recurse."""
    held = retain_regular_file(directory.path / name)
    try:
        if os.name != "nt":
            assert directory.fd is not None
            os.unlink(name, dir_fd=directory.fd)
            os.fsync(directory.fd)
        else:
            os.unlink(directory.path / name)
            if not _same_directory_path(directory):
                raise OSError("retained Windows directory changed during unlink")
    finally:
        held.close()


def retained_regular_child_names(directory: _SecureDirectory, names: tuple[str, ...]) -> tuple[str, ...]:
    """Census only exact regular no-follow children under one pinned parent."""
    if not _same_directory_path(directory):
        raise OSError("retained directory changed")
    present: list[str] = []
    if os.name != "nt":
        assert directory.fd is not None
        entries = set(os.listdir(directory.fd))
        for name in names:
            if name not in entries:
                continue
            fd = _open_secure_file_at(directory, name, os.O_RDONLY)
            try:
                if not _same_file_entry(directory, name, fd):
                    raise OSError("retained child identity changed")
            finally:
                os.close(fd)
            present.append(name)
        return tuple(present)
    for name in names:
        try:
            held = retain_regular_file(directory.path / name)
        except FileNotFoundError:
            continue
        try:
            if not _same_directory_path(directory):
                raise OSError("retained Windows parent changed during census")
            present.append(name)
        finally:
            held.close()
    return tuple(present)


def _open_secure_file_at(
    directory: _SecureDirectory,
    name: str,
    flags: int,
    mode: int = 0o600,
    *,
    delete_access: bool = False,
) -> int:
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise OSError("lock operations require one child basename")
    if os.name != "nt":
        actual_flags = flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(name, actual_flags, mode, dir_fd=directory.fd)
    else:
        access = 0x80000000  # GENERIC_READ
        if flags & os.O_RDWR:
            access = 0xC0000000  # GENERIC_READ | GENERIC_WRITE
        elif flags & os.O_WRONLY:
            access = 0x40000000  # GENERIC_WRITE
        if delete_access:
            access |= 0x00010000  # DELETE, required by SetFileInformationByHandle
        creation = 4 if flags & os.O_CREAT else 3  # OPEN_ALWAYS / OPEN_EXISTING
        handle = _windows_open_path(
            directory.path / name,
            directory=False,
            access=access,
            creation=creation,
        )
        try:
            if not _windows_child_is_in_directory(directory, handle):
                raise OSError("Windows runtime file escaped its retained directory")
            fd = getattr(msvcrt, "open_osfhandle")(handle, flags | getattr(os, "O_BINARY", 0))
        except BaseException:
            _windows_close_handle(handle)
            raise
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise OSError("lock path is not a regular file")
    return fd


def _open_owned_runtime_lock_file(directory: _SecureDirectory, name: str) -> int:
    """Open one persistent owner-only runtime lock without replacing it."""
    created = False
    if os.name != "nt":
        try:
            fd = _open_secure_file_at(directory, name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
        except FileExistsError:
            fd = _open_secure_file_at(directory, name, os.O_RDWR, 0o600)
        try:
            if created:
                os.fchmod(fd, 0o600)
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise OSError("runtime lock file is not owner-only")
            return fd
        except BaseException:
            os.close(fd)
            raise
    return _open_secure_file_at(directory, name, os.O_RDWR | os.O_CREAT, 0o600)


@dataclass
class _LocalLockState:
    guard: threading.RLock = field(default_factory=threading.RLock)
    metadata_guard: threading.Lock = field(default_factory=threading.Lock)
    owner_thread: int | None = None
    depth: int = 0
    handle: BinaryIO | None = None
    request_id: str | None = None
    operation: str | None = None
    holder_kind: str | None = None
    acquired_at: float | None = None
    long_holder_seconds: float = _DEFAULT_LONG_HOLDER_SECONDS
    long_warning_emitted: bool = False


_LOCAL_STATES: dict[Path, _LocalLockState] = {}
_LOCAL_STATES_GUARD = threading.Lock()


def _state_for(lock_path: Path) -> _LocalLockState:
    with _LOCAL_STATES_GUARD:
        state = _LOCAL_STATES.get(lock_path)
        if state is None:
            state = _LocalLockState()
            _LOCAL_STATES[lock_path] = state
        return state


def _reset_in_forked_child() -> None:
    """Drop inherited thread state and close inherited lock descriptors.

    Closing an inherited descriptor in the child does not release the parent's
    descriptor.  It prevents a forked child from retaining the parent's OS lock
    indefinitely while giving the child fresh process-local ``RLock`` objects.
    """
    global _LOCAL_STATES, _LOCAL_STATES_GUARD
    for state in _LOCAL_STATES.values():
        if state.handle is not None:
            try:
                state.handle.close()
            except OSError:
                pass
    _LOCAL_STATES = {}
    _LOCAL_STATES_GUARD = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_in_forked_child)


class VaultMutationCoordinator:
    """Serialize the complete read-plan-write boundary for one vault or cell."""

    def __init__(
        self,
        state_root: Path,
        vault_or_cell: os.PathLike[str] | str,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        long_holder_seconds: float = _DEFAULT_LONG_HOLDER_SECONDS,
    ) -> None:
        if timeout_seconds < 0:
            raise ValueError("mutation lock timeout must be non-negative")
        if poll_interval_seconds <= 0:
            raise ValueError("mutation lock poll interval must be positive")
        if long_holder_seconds <= 0:
            raise ValueError("mutation long-holder threshold must be positive")
        self.state_root = Path(state_root).expanduser().resolve(strict=False)
        self.identity = canonical_mutation_identity(vault_or_cell)
        digest = hashlib.sha256(self.identity.encode("utf-8")).hexdigest()
        lock_root = self.state_root / "mutation-locks"
        self.lock_path = lock_root / f"{digest}.lock"
        self.metadata_lock_path = lock_root / f"{digest}.metadata.lock"
        self.metadata_path = lock_root / f"{digest}.holder.json"
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.long_holder_seconds = long_holder_seconds

    @contextmanager
    def hold(
        self,
        *,
        timeout_seconds: float | None = None,
        request_id: str | None = None,
        operation: str | None = None,
        holder_kind: str = "unknown",
    ) -> Iterator[None]:
        """Hold both the local and OS mutation guards for the bounded interval."""
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout < 0:
            raise ValueError("mutation lock timeout must be non-negative")
        wait_start = time.monotonic()
        # Clear any prior hold's timing so a mutation that fails BEFORE
        # acquiring can never journal a previous mutation's wait/hold values.
        _LAST_MUTATION_TIMING.set(None)
        deadline = wait_start + timeout
        state = _state_for(self.lock_path)
        remaining = max(0.0, deadline - time.monotonic())
        if not state.guard.acquire(timeout=remaining):
            raise _mutation_busy(
                self.snapshot(), wait_ms=(time.monotonic() - wait_start) * 1000
            )
        try:
            thread_id = threading.get_ident()
            if state.owner_thread == thread_id:
                state.depth += 1
                try:
                    yield
                finally:
                    state.depth -= 1
                return

            handle = self._open_lock_file(self.lock_path)
            metadata_handle = self._open_lock_file(self.metadata_lock_path)
            try:
                self._acquire_boundary(
                    handle, metadata_handle, deadline, request_id, operation, holder_kind,
                    wait_start=wait_start,
                )
            except Exception:
                handle.close()
                metadata_handle.close()
                raise
            acquired_at = time.monotonic()
            wait_ms = round((acquired_at - wait_start) * 1000, 2)
            state.owner_thread = thread_id
            state.depth = 1
            state.handle = handle
            with state.metadata_guard:
                state.request_id = _safe_label(request_id, fallback="untracked")
                state.operation = _safe_label(operation, fallback="unknown")
                state.holder_kind = _safe_label(holder_kind, fallback="unknown")
                state.acquired_at = acquired_at
                state.long_holder_seconds = self.long_holder_seconds
                state.long_warning_emitted = False
            _log_mutation_lock_event(
                "mutation_lock_acquired",
                operation=operation,
                holder_kind=holder_kind,
                wait_ms=wait_ms,
            )
            _observe_boundary_ms("exomem_boundary_wait_ms", wait_ms)
            try:
                yield
            finally:
                hold_ms = round((time.monotonic() - acquired_at) * 1000, 2)
                _LAST_MUTATION_TIMING.set(
                    {
                        "wait_ms": wait_ms,
                        "hold_ms": hold_ms,
                        "operation": _safe_label(operation, fallback="unknown"),
                        "holder_kind": _safe_label(holder_kind, fallback="unknown"),
                    }
                )
                with state.metadata_guard:
                    already_warned = state.long_warning_emitted
                    state.request_id = None
                    state.operation = None
                    state.holder_kind = None
                    state.acquired_at = None
                    state.long_warning_emitted = False
                # Reset reentrancy state BEFORE releasing the OS lock: a stale
                # owner_thread would route this thread's next hold() through the
                # reentrant fast path with no OS lock at all.
                state.depth = 0
                state.owner_thread = None
                state.handle = None
                try:
                    self._release_boundary(handle, metadata_handle)
                finally:
                    handle.close()
                    metadata_handle.close()
                # Telemetry AFTER release, so log appends never extend the
                # critical section other writers are polling on. Guaranteed on
                # release, regardless of whether anyone ever probed this hold
                # while it was live (`_snapshot_state`'s warning only fires
                # when something calls `snapshot()`).
                overdue = hold_ms >= self.long_holder_seconds * 1000
                if not already_warned and overdue:
                    logger.warning(
                        "vault mutation boundary held too long request_id=%s operation=%s "
                        "holder_kind=%s hold_ms=%.2f",
                        _safe_label(request_id, fallback="untracked"),
                        _safe_label(operation, fallback="unknown"),
                        _safe_label(holder_kind, fallback="unknown"),
                        hold_ms,
                    )
                    _log_mutation_lock_event(
                        "mutation_lock_long_hold",
                        operation=operation,
                        holder_kind=holder_kind,
                        hold_ms=hold_ms,
                    )
                if overdue:
                    _bump_boundary_metric("exomem_boundary_overdue_total")
                _log_mutation_lock_event(
                    "mutation_lock_released",
                    operation=operation,
                    holder_kind=holder_kind,
                    hold_ms=hold_ms,
                )
                _observe_boundary_ms("exomem_boundary_hold_ms", hold_ms)
        finally:
            state.guard.release()

    def snapshot(self) -> dict[str, object]:
        """Measure this vault's OS boundary without exposing identity or content."""
        state = _state_for(self.lock_path)
        local = _snapshot_state(state, emit_warning=True)
        if local["state"] == "held":
            return local

        metadata_handle = self._open_lock_file(self.metadata_lock_path)
        try:
            mutation_handle = self._open_lock_file(self.lock_path)
        except Exception:
            metadata_handle.close()
            raise
        metadata_locked = False
        mutation_locked = False
        try:
            metadata_locked = _acquire_os_lock_until(
                metadata_handle,
                time.monotonic() + _STATUS_TIMEOUT_SECONDS,
                self.poll_interval_seconds,
            )
            if not metadata_locked:
                # The metadata mutex is genuinely contended (not just briefly
                # held during a publish). Rather than fabricate a healthy-
                # looking unknown holder at age 0, fall back to a lock-free
                # read of the sidecar: it is published via atomic
                # `os.replace`, so a read without the mutex is tear-free even
                # though it cannot be verified current. Only the genuine
                # absence of any sidecar still fabricates an unknown holder.
                holder = _read_holder_metadata(self.metadata_path)
                if holder is not None:
                    _log_mutation_lock_event(
                        "mutation_holder_unverified",
                        holder_kind=holder.get("holder_kind"),
                    )
                    return _holder_snapshot(holder, verified=False)
                return _unknown_external_holder()
            mutation_locked = _try_os_lock(mutation_handle)
            if mutation_locked:
                _clear_holder_metadata(self.metadata_path)
                return {"state": "free"}
            holder = _read_holder_metadata(self.metadata_path)
            if holder is None:
                return _unknown_external_holder()
            return _holder_snapshot(holder, verified=True)
        except OSError as exc:
            raise self._lock_unavailable(exc) from None
        finally:
            if mutation_locked:
                try:
                    _release_os_lock(mutation_handle)
                except OSError:
                    pass
            if metadata_locked:
                try:
                    _release_os_lock(metadata_handle)
                except OSError:
                    pass
            mutation_handle.close()
            metadata_handle.close()

    def _open_lock_file(self, path: Path) -> BinaryIO:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+b")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            return handle
        except OSError as exc:
            _bump_boundary_metric(
                "exomem_mutation_busy_total", {"code": "MUTATION_LOCK_UNAVAILABLE"}
            )
            raise OpError(
                "MUTATION_LOCK_UNAVAILABLE",
                f"vault mutation lock could not be opened (host error {exc.errno})",
                "Check that the configured runtime state root exists and is writable.",
            ) from None

    def _acquire_boundary(
        self,
        handle: BinaryIO,
        metadata_handle: BinaryIO,
        deadline: float,
        request_id: str | None,
        operation: str | None,
        holder_kind: str,
        *,
        wait_start: float,
    ) -> None:
        """Acquire and publish with the metadata mutex held for one generation."""
        while True:
            try:
                metadata_locked = _acquire_os_lock_until(
                    metadata_handle, deadline, self.poll_interval_seconds
                )
            except OSError as exc:
                raise self._lock_unavailable(exc) from None
            if not metadata_locked:
                raise _mutation_busy(
                    self.snapshot(), wait_ms=(time.monotonic() - wait_start) * 1000
                )
            acquired = False
            try:
                try:
                    acquired = _try_os_lock(handle)
                except OSError as exc:
                    raise self._lock_unavailable(exc) from None
                if acquired:
                    holder = {
                        "schema": _HOLDER_SCHEMA,
                        "generation": uuid.uuid4().hex,
                        "request_id": _safe_label(request_id, fallback="untracked"),
                        "operation": _safe_label(operation, fallback="unknown"),
                        "holder_kind": _safe_label(holder_kind, fallback="unknown"),
                        "acquired_at": time.time(),
                        "long_holder_seconds": self.long_holder_seconds,
                    }
                    try:
                        self._publish_holder_metadata(holder)
                    except OSError as exc:
                        _clear_holder_metadata(self.metadata_path)
                        try:
                            _release_os_lock(handle)
                        except OSError:
                            pass
                        raise self._lock_unavailable(exc) from None
                    return
            finally:
                try:
                    _release_os_lock(metadata_handle)
                except OSError:
                    pass
            if acquired:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _mutation_busy(
                    self.snapshot(), wait_ms=(time.monotonic() - wait_start) * 1000
                )
            time.sleep(min(self.poll_interval_seconds, remaining))

    def _publish_holder_metadata(self, holder: dict[str, object]) -> None:
        _atomic_write_holder_metadata(self.metadata_path, holder)

    def _release_boundary(
        self, handle: BinaryIO, metadata_handle: BinaryIO
    ) -> None:
        """Clear and unlock under the mutex; stale metadata remains crash-safe."""
        metadata_locked = False
        try:
            try:
                metadata_locked = _acquire_os_lock_until(
                    metadata_handle,
                    math.inf,
                    self.poll_interval_seconds,
                )
            except OSError:
                # Descriptor close still releases authority. Do not mask a
                # committed result or leaf exception with host cleanup detail.
                logger.exception("mutation metadata mutex failed during release")
            if metadata_locked:
                try:
                    _clear_holder_metadata(self.metadata_path)
                except OSError:
                    # Unlock regardless. The next successful boundary probe
                    # distinguishes and removes stale metadata safely.
                    pass
            try:
                _release_os_lock(handle)
            except OSError:
                # Closing the descriptor releases OS ownership. A stale sidecar
                # is removed by the next successful status/acquisition probe.
                pass
        finally:
            if metadata_locked:
                try:
                    _release_os_lock(metadata_handle)
                except OSError:
                    pass

    @staticmethod
    def _lock_unavailable(exc: OSError) -> OpError:
        _bump_boundary_metric(
            "exomem_mutation_busy_total", {"code": "MUTATION_LOCK_UNAVAILABLE"}
        )
        return OpError(
            "MUTATION_LOCK_UNAVAILABLE",
            f"vault mutation authority could not be established (host error {exc.errno})",
            "Check runtime state storage and the host locking implementation.",
        )


def _try_os_lock(
    handle: BinaryIO,
    *,
    _windows: bool = os.name == "nt",
    _locking: Callable[[int, int, int], Any] | None = (
        getattr(msvcrt, "locking", None) if os.name == "nt" else None
    ),
    _flock: Callable[[int, int], Any] | None = (
        None if os.name == "nt" else fcntl.flock
    ),
    _busy_errnos: frozenset[int] = _BUSY_ERRNOS,
    _lock_nonblocking: int | None = (
        getattr(msvcrt, "LK_NBLCK", None) if os.name == "nt" else None
    ),
    _lock_exclusive_nonblocking: int | None = (
        None if os.name == "nt" else fcntl.LOCK_EX | fcntl.LOCK_NB
    ),
) -> bool:
    try:
        if _windows:
            assert _locking is not None and _lock_nonblocking is not None
            handle.seek(0)
            _locking(handle.fileno(), _lock_nonblocking, 1)
        else:
            assert _flock is not None and _lock_exclusive_nonblocking is not None
            _flock(handle.fileno(), _lock_exclusive_nonblocking)
    except BlockingIOError:
        return False
    except OSError as exc:
        if exc.errno in _busy_errnos:
            return False
        raise
    return True


def _acquire_os_lock_until(
    handle: BinaryIO, deadline: float, poll_interval_seconds: float
) -> bool:
    """Acquire one OS lock by a deadline without hiding host-lock failures."""
    while True:
        if _try_os_lock(handle):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_interval_seconds, remaining))


def _release_os_lock(
    handle: BinaryIO,
    *,
    _windows: bool = os.name == "nt",
    _locking: Callable[[int, int, int], Any] | None = (
        getattr(msvcrt, "locking", None) if os.name == "nt" else None
    ),
    _flock: Callable[[int, int], Any] | None = (
        None if os.name == "nt" else fcntl.flock
    ),
    _lock_unlock: int | None = (
        getattr(msvcrt, "LK_UNLCK", None) if os.name == "nt" else None
    ),
    _lock_unlock_posix: int | None = (None if os.name == "nt" else fcntl.LOCK_UN),
) -> None:
    if _windows:
        assert _locking is not None and _lock_unlock is not None
        handle.seek(0)
        _locking(handle.fileno(), _lock_unlock, 1)
    else:
        assert _flock is not None and _lock_unlock_posix is not None
        _flock(handle.fileno(), _lock_unlock_posix)


def _safe_label(value: object, *, fallback: str) -> str:
    candidate = str(value or "").strip()
    return candidate if _SAFE_LABEL.fullmatch(candidate) else fallback


def _atomic_write_holder_metadata(path: Path, holder: dict[str, object]) -> None:
    """Atomically publish the content-free holder record in local runtime state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(holder, sort_keys=True, separators=(",", ":"))
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _clear_holder_metadata(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _read_holder_metadata(path: Path) -> dict[str, object] | None:
    try:
        value: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("schema") != _HOLDER_SCHEMA:
        return None
    generation = value.get("generation")
    request_id = value.get("request_id")
    operation = value.get("operation")
    holder_kind = value.get("holder_kind")
    acquired_at = value.get("acquired_at")
    long_holder_seconds = value.get("long_holder_seconds")
    if not (
        isinstance(generation, str)
        and re.fullmatch(r"[0-9a-f]{32}", generation)
        and isinstance(request_id, str)
        and _SAFE_LABEL.fullmatch(request_id)
        and isinstance(operation, str)
        and _SAFE_LABEL.fullmatch(operation)
        and isinstance(holder_kind, str)
        and _SAFE_LABEL.fullmatch(holder_kind)
        and isinstance(acquired_at, (int, float))
        and not isinstance(acquired_at, bool)
        and math.isfinite(acquired_at)
        and isinstance(long_holder_seconds, (int, float))
        and not isinstance(long_holder_seconds, bool)
        and math.isfinite(long_holder_seconds)
        and long_holder_seconds > 0
    ):
        return None
    return value


def _holder_snapshot(
    holder: dict[str, object], *, verified: bool
) -> dict[str, object]:
    acquired_at = float(cast(int | float, holder["acquired_at"]))
    long_holder_seconds = float(cast(int | float, holder["long_holder_seconds"]))
    age = max(0.0, time.time() - acquired_at)
    return {
        "state": "held",
        "request_id": str(holder["request_id"]),
        "operation": str(holder["operation"]),
        "holder_kind": str(holder["holder_kind"]),
        "age_seconds": round(age, 3),
        "overdue": age >= long_holder_seconds,
        "verified": verified,
    }


def _unknown_external_holder() -> dict[str, object]:
    return {
        "state": "held",
        "request_id": "untracked",
        "operation": "unknown",
        "holder_kind": "external",
        "age_seconds": 0.0,
        "overdue": False,
        "verified": False,
    }


def _snapshot_state(
    state: _LocalLockState, *, emit_warning: bool
) -> dict[str, object]:
    with state.metadata_guard:
        if state.owner_thread is None or state.acquired_at is None:
            return {"state": "free"}
        age = max(0.0, time.monotonic() - state.acquired_at)
        overdue = age >= state.long_holder_seconds
        snapshot: dict[str, object] = {
            "state": "held",
            "request_id": state.request_id or "untracked",
            "operation": state.operation or "unknown",
            "holder_kind": state.holder_kind or "unknown",
            "age_seconds": round(age, 3),
            "overdue": overdue,
            "verified": True,
        }
        if overdue and emit_warning and not state.long_warning_emitted:
            state.long_warning_emitted = True
            logger.warning(
                "vault mutation boundary held too long request_id=%s operation=%s "
                "holder_kind=%s age_seconds=%.3f",
                snapshot["request_id"],
                snapshot["operation"],
                snapshot["holder_kind"],
                age,
            )
        return snapshot


def active_mutation_snapshot() -> dict[str, object]:
    """Return the oldest process-local holder without exposing vault identity."""
    with _LOCAL_STATES_GUARD:
        states = tuple(_LOCAL_STATES.values())
    held = [
        snapshot
        for state in states
        if (snapshot := _snapshot_state(state, emit_warning=True))["state"] == "held"
    ]
    if not held:
        return {"state": "free"}
    return max(
        held,
        key=lambda item: float(cast(int | float, item["age_seconds"])),
    )


def dynamic_retry_after_ms(snapshot: dict[str, object] | None) -> int:
    """Scale the retry hint with observed contention instead of a fixed 750ms.

    `min(15000, max(750, age_seconds*500))`, floored at 5000 when the holder
    is overdue; 750 when there is no known holder (age 0 / no snapshot).
    """
    if not snapshot or snapshot.get("state") != "held":
        return 750
    age_seconds = float(cast(int | float, snapshot.get("age_seconds") or 0.0))
    retry_after_ms = min(15000, max(750, int(age_seconds * 500)))
    if snapshot.get("overdue"):
        retry_after_ms = max(retry_after_ms, 5000)
    return retry_after_ms


def _mutation_busy(
    snapshot: dict[str, object] | None = None, *, wait_ms: float | None = None
) -> OpError:
    details: dict[str, object] = {
        "status": "retryable",
        "committed": False,
        "retry_after_ms": dynamic_retry_after_ms(snapshot),
    }
    if wait_ms is not None:
        details["wait_ms"] = round(wait_ms, 2)
    if snapshot and snapshot.get("state") == "held":
        details["holder"] = snapshot
    _bump_boundary_metric("exomem_mutation_busy_total", {"code": "MUTATION_BUSY"})
    return OpError(
        "MUTATION_BUSY",
        "vault mutation boundary is busy",
        "Retry after the current mutation completes; inspect cell health if it remains busy.",
        details=details,
    )
