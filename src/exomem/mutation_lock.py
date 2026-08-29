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
from collections import deque
from collections.abc import Callable, Iterator, Mapping
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
# Contention attribution window.  A boundary flag can never explain a stream of
# short holds starving a bounded waiter: every one-shot probe lands in a gap and
# reads "free" while acquires are really being refused.  Counters and a
# last-known holder can, so they are kept per boundary and surfaced alongside
# the flag.
_CONTENTION_RECENT_WINDOW_SECONDS = 60.0
_CONTENTION_RECENT_SAMPLES = 128
_WINDOWS_DACL_STABILIZATION_SECONDS = 0.25
_WINDOWS_DACL_STABILIZATION_POLL_SECONDS = 0.01

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
    owns_directory: bool = True

    def close(self) -> None:
        try:
            os.close(self.fd)
        finally:
            if self.owns_directory:
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
                identity = (
                    int(info.st_dev),
                    int(info.st_ino),
                    int(info.st_mode),
                    int(info.st_size),
                    int(info.st_mtime_ns),
                )
            return RetainedRegularFile(target, directory, fd, identity)
        except BaseException:
            os.close(fd)
            raise
    except BaseException:
        directory.close()
        raise


def retain_regular_child_file(directory: _SecureDirectory, name: str) -> RetainedRegularFile:
    """Pin one exact regular child while its caller keeps the parent retained."""
    if not name or Path(name).name != name or "/" in name or "\\" in name:
        raise OSError("retained file child must be one basename")
    if not _same_directory_path(directory):
        raise OSError("retained file parent changed")
    fd = _open_secure_file_at(directory, name, os.O_RDONLY, delete_access=True)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or not _same_file_entry(directory, name, fd):
            raise OSError("regular file changed while being retained")
        if os.name == "nt":
            import msvcrt

            identity: tuple[int, ...] = _windows_handle_identity(msvcrt.get_osfhandle(fd))
        else:
            identity = (
                int(info.st_dev),
                int(info.st_ino),
                int(info.st_mode),
                int(info.st_size),
                int(info.st_mtime_ns),
            )
        return RetainedRegularFile(directory.path / name, directory, fd, identity, owns_directory=False)
    except BaseException:
        os.close(fd)
        raise


def _windows_rename_handle(handle: int, directory: _SecureDirectory, name: str, *, replace: bool) -> None:
    """Move one exact native handle into its retained parent and prove identity."""
    import ctypes
    from ctypes import wintypes

    class _RenameInfo(ctypes.Structure):
        _fields_ = [
            ("replace", wintypes.BOOLEAN),
            ("root", wintypes.HANDLE),
            ("length", wintypes.DWORD),
            ("filename", wintypes.WCHAR * 1),
        ]

    encoded = name.encode("utf-16-le")
    filename_offset = _RenameInfo.filename.offset
    # FILE_RENAME_INFO declares a trailing WCHAR[1]; SetFileInformationByHandle
    # requires that declared element as well as the variable-length name bytes.
    size = ctypes.sizeof(_RenameInfo) + len(encoded)
    raw = ctypes.create_string_buffer(size)
    info = _RenameInfo.from_buffer(raw)
    info.replace = replace
    info.root = directory.windows_handle
    info.length = len(encoded)
    ctypes.memmove(ctypes.addressof(raw) + filename_offset, encoded, len(encoded))
    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", ctypes.c_long), ("information", ctypes.c_size_t)]

    status = _IoStatusBlock()
    ntdll = _windows_library(ctypes, "ntdll")
    rename = ntdll.NtSetInformationFile
    rename.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_IoStatusBlock), ctypes.c_void_p,
        wintypes.ULONG, ctypes.c_int,
    ]
    rename.restype = ctypes.c_long
    code = rename(handle, ctypes.byref(status), raw, size, 10)  # FileRenameInformation
    if code < 0:
        raise OSError(int(code), "retained Windows rename refused")


def _windows_delete_handle(handle: int) -> None:
    """Mark the exact DELETE-capable file handle for deletion on close."""
    import ctypes
    from ctypes import wintypes

    delete = wintypes.BOOLEAN(True)
    kernel32 = _windows_library(ctypes, "kernel32")
    disposition = kernel32.SetFileInformationByHandle
    disposition.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    disposition.restype = wintypes.BOOL
    if not disposition(handle, 4, ctypes.byref(delete), ctypes.sizeof(delete)):
        raise OSError(_windows_last_error(ctypes), "retained Windows delete refused")


def rename_retained_regular_file(
    source: RetainedRegularFile, destination: Path, *, destination_directory: _SecureDirectory | None = None
) -> None:
    """Rename the exact retained source to a new sibling in a retained directory."""
    target = Path(destination)
    if target.name != target.as_posix().split("/")[-1]:
        raise OSError("retained rename destination must be one child basename")
    directory = destination_directory or _acquire_secure_directory(target.parent, create=False)
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
        import msvcrt
        _windows_rename_handle(msvcrt.get_osfhandle(source.fd), directory, target.name, replace=False)
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
        if destination_directory is None:
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


class WindowsReparsePointError(OSError):
    """The opened entry is a reparse point (symlink/junction), which is refused.

    An `OSError` subclass so every existing `except OSError` keeps catching it.
    It exists so a caller can tell "this path is an alias" from "another
    principal denied me": both used to surface as a bare `OSError` here, and a
    junction standing in for the state root was then diagnosed as a
    cross-principal DACL problem with an `icacls` remediation that cannot fix
    it.
    """


class WindowsPathTypeError(OSError):
    """The entry exists but is a file where a directory was required, or vice versa."""


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
            raise WindowsReparsePointError("reparse points are not allowed")
        if bool(info.attributes & 0x10) != directory:
            raise WindowsPathTypeError("unexpected path type")
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


#: SDDL two-letter aliases Windows substitutes for well-known SIDs. The choice
#: is the renderer's, not ours: the same principal reaches us as a raw SID from
#: one source and as its alias from another, so any comparison has to admit both
#: spellings or it silently rejects the principal it was written to accept.
_WINDOWS_SID_ALIASES = {"S-1-5-18": "SY", "S-1-5-32-544": "BA"}

#: OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION. The owner is not
#: decoration here. An `OWNER RIGHTS` ACE grants to whoever currently owns the
#: object, so a DACL read on its own cannot say who the DACL admits.
_WINDOWS_OWNER_AND_DACL_INFORMATION = 0x00000001 | 0x00000004

#: `OWNER RIGHTS` (S-1-3-4), which SDDL renders as `OW`. An ACE naming it grants
#: to the object's current owner rather than to any fixed principal.
_WINDOWS_OWNER_RIGHTS_TRUSTEES = frozenset({"ow", "s-1-3-4"})


def _windows_sddl_owner(sddl: str) -> str | None:
    """Return the owner principal an SDDL string declares, if it carries one.

    Owner may be rendered as a raw SID or as a two-letter alias, and it runs up
    against the following `G:`/`D:`/`S:` with no separator, so the SID branch
    has to stop on its own rather than on a delimiter.
    """
    match = re.match(r"O:(S-1-[0-9]+(?:-[0-9]+)*|[A-Za-z]{2})", sddl)
    return match.group(1) if match else None


#: SDDL renders the local domain's built-in Administrator -- the RID 500 account
#: -- as `LA`, never as its SID. The alias is machine-relative, so unlike `SY`
#: and `BA` it cannot live in `_WINDOWS_SID_ALIASES`; it has to be derived from
#: the current SID. A host whose current user *is* that account therefore reads
#: its own grants back under a spelling the raw-SID comparison cannot match.
_WINDOWS_LOCAL_ADMIN_PREFIX = "S-1-5-21-"
_WINDOWS_LOCAL_ADMIN_SUFFIX = "-500"


def _windows_is_local_admin_sid(sid: str) -> bool:
    """True when *sid* is an account domain's built-in Administrator (RID 500)."""
    return sid.startswith(_WINDOWS_LOCAL_ADMIN_PREFIX) and sid.endswith(
        _WINDOWS_LOCAL_ADMIN_SUFFIX
    )


def _windows_principal_spellings(sid: str) -> frozenset[str]:
    """Every casefolded spelling that denotes *sid* in an SDDL string."""
    spellings = {sid.casefold(), _WINDOWS_SID_ALIASES.get(sid, sid).casefold()}
    if _windows_is_local_admin_sid(sid):
        spellings.add("la")
    return frozenset(spellings)


#: Every casefolded spelling of BUILTIN\Administrators, the group an account
#: domain's RID 500 belongs to by construction.
_WINDOWS_ADMINISTRATORS_SPELLINGS = frozenset({"ba", "s-1-5-32-544"})


def _windows_owner_admits_current_user(owner: str | None, sid: str) -> bool:
    r"""True when an `OW` ACE on an object owned by *owner* grants to *sid*.

    The literal case is an owner spelled as the current user. The other case is
    a policy default rather than an oddity: where "System objects: Default owner
    for objects created by members of the Administrators group" names the group,
    every directory an administrator creates is owned by
    BUILTIN\Administrators, not by the account. GitHub's `windows-latest`
    runners are configured that way, so a directory this module created itself
    -- `D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)`, the exact shape the
    validator documents as correct -- read back as owned by `BA` and failed.

    The built-in Administrator is a member of that group by construction, so an
    `OW` ACE there does grant to us. It grants to the rest of the group too, and
    that is not a widening: `BA` is already one of the three trustees private
    runtime state permits, so this admits nobody the DACL did not already admit.

    Deliberately not folded into `_windows_principal_spellings`. That set also
    resolves *literal* ACE trustees, and adding `BA` to it would collapse a real
    `BA` grant onto the user's slot -- losing the distinction between the two
    principals the equality check depends on.
    """
    if owner is None:
        return False
    spelling = owner.casefold()
    if spelling in _windows_principal_spellings(sid):
        return True
    return _windows_is_local_admin_sid(sid) and spelling in _WINDOWS_ADMINISTRATORS_SPELLINGS


def _windows_private_dacl_trustees(sid: str) -> tuple[str, ...]:
    """Return the distinct SDDL trustees permitted for private runtime state."""
    if not re.fullmatch(r"S-1-[0-9-]+", sid):
        raise ValueError("invalid current Windows SID")
    principals = (_WINDOWS_SID_ALIASES.get(sid, sid), "SY", "BA")
    unique: dict[str, str] = {}
    for principal in principals:
        unique.setdefault(principal.casefold(), principal)
    return tuple(unique.values())


class WindowsRuntimeDaclError(RuntimeError):
    """Actionable fail-closed result for one unsafe idempotency runtime entry.

    Carries the descriptor it rejected. Without that the failure is only
    diagnosable on a machine you can log into: the message named a path and a
    repair command but never what it actually observed, so a report from another
    host -- a CI runner, a user -- could not be acted on, and the shape had to be
    guessed at from whatever a local machine happened to produce. Guessing wrong
    is cheap to do and expensive to discover.
    """

    def __init__(
        self,
        path: Path,
        remediation: str,
        *,
        observed: str | None = None,
        expected: tuple[str, ...] = (),
    ):
        self.path = path
        self.remediation = remediation
        self.observed = observed
        self.expected = expected
        detail = ""
        if observed is not None:
            detail = f"; observed {observed!r}"
        if expected:
            detail += f"; expected full-access trustees {', '.join(expected)}"
        super().__init__(
            f"unsafe Windows DACL at {path}{detail}; "
            f"run in elevated PowerShell: {remediation}"
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


def _windows_apply_dacl_sddl(path: Path, sddl: str, *, propagate: bool = False) -> None:
    """Apply one native SDDL DACL without shelling out through a localized tool.

    ``propagate`` picks the API, and the choice is load-bearing for any entry
    that already has children. ``SetFileSecurityW`` writes the one object and
    leaves existing children alone -- and because protecting a DACL strips the
    parent's inheritance, Windows CONVERTS each child's previously-inherited
    ACEs into explicit ones so they survive. Measured on a state root sealed to
    SYSTEM: the child went from ``(A;ID;FA;;;<user>)`` to ``(A;;FA;;;<user>)``
    and stayed fully readable and writable by the operator, while the directory
    itself refused a listing -- a root that LOOKS sealed over state that is not.

    ``SetNamedSecurityInfoW`` recomputes inheritance across the tree instead, so
    the same child became ``AI(A;ID;FA;;;SY)(A;ID;FA;;;BA)`` and every operator
    read, write and create was denied.

    Creation paths keep the cheaper call deliberately: the directory their own
    ``mkdir`` just made has no children to propagate to, and they are the
    long-tested idempotency and writer-lease paths.
    """
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
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
        dacl_information = 0x00000004 | 0x80000000
        if not propagate:
            set_security = advapi32.SetFileSecurityW
            set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
            set_security.restype = wintypes.BOOL
            if not set_security(str(path), dacl_information, descriptor):
                raise OSError(_windows_last_error(ctypes), "cannot protect Windows runtime DACL")
            return
        dacl = wintypes.LPVOID()
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        get_dacl = advapi32.GetSecurityDescriptorDacl
        get_dacl.argtypes = [
            wintypes.LPVOID, ctypes.POINTER(wintypes.BOOL),
            ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.BOOL),
        ]
        get_dacl.restype = wintypes.BOOL
        if not get_dacl(descriptor, ctypes.byref(present), ctypes.byref(dacl),
                        ctypes.byref(defaulted)):
            raise OSError(_windows_last_error(ctypes), "cannot read Windows runtime DACL")
        set_named = advapi32.SetNamedSecurityInfoW
        set_named.argtypes = [
            wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, wintypes.LPVOID,
            wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID,
        ]
        set_named.restype = wintypes.DWORD
        result = set_named(str(path), 1, dacl_information, None, None, dacl, None)
        if result:
            raise OSError(result, "cannot protect Windows runtime DACL tree")
    finally:
        kernel32.LocalFree(descriptor)


def _windows_apply_private_dacl(path: Path, sid: str, *, propagate: bool = False) -> None:
    """Set a protected inheritable DACL on a newly-created runtime directory."""
    _windows_apply_dacl_sddl(path, _windows_private_dacl_sddl(sid), propagate=propagate)


def _windows_tighten_private_directory(
    target: Path, directory: _SecureDirectory, sid: str
) -> None:
    """Protect a directory whose DACL already admits only the private trustees.

    This is not the repair the module declines to perform. That one would rewrite
    an entry whose ACEs let somebody else in, which cannot help: an ACL change
    does not close a handle already opened against the old one, so the attacker
    keeps the access and we would proceed believing otherwise. Here the caller
    has established the opposite -- every trustee is one this module writes
    itself -- so there is no such handle to close, and protecting the DACL only
    removes the parent's future reach into it.

    Applied by path, then proven through the retained handle by the caller's
    re-validation. A path swapped in between gets our protected DACL, which is
    harmless to us, while the handle we still hold reports the object we
    validated -- so a swap fails the re-validation instead of passing it.
    """
    if not _same_directory_path(directory):
        raise OSError("Windows directory changed before its DACL was protected")
    _windows_apply_private_dacl(target, sid)


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
    result = get_security(
        str(path), 1, _WINDOWS_OWNER_AND_DACL_INFORMATION, None, None, None, None,
        ctypes.byref(security_descriptor),
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
        if not convert(
            security_descriptor, 1, _WINDOWS_OWNER_AND_DACL_INFORMATION,
            ctypes.byref(text), ctypes.byref(size),
        ):
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
    result = get_security(
        handle, 1, _WINDOWS_OWNER_AND_DACL_INFORMATION, None, None, None, None,
        ctypes.byref(security_descriptor),
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
        if not convert(
            security_descriptor, 1, _WINDOWS_OWNER_AND_DACL_INFORMATION,
            ctypes.byref(text), ctypes.byref(size),
        ):
            raise OSError(_windows_last_error(ctypes), "cannot render Windows runtime DACL")
        try:
            return str(text.value)
        finally:
            kernel32.LocalFree(text)
    finally:
        kernel32.LocalFree(security_descriptor)


_WINDOWS_DACL_PRIVATE = "private"
_WINDOWS_DACL_INHERITED = "inherited"
_WINDOWS_DACL_UNSAFE = "unsafe"


def _windows_private_dacl_is_valid(sddl: str, sid: str, *, directory: bool) -> bool:
    """True only for a DACL this module would have written itself."""
    return _windows_private_dacl_verdict(sddl, sid, directory=directory) == (
        _WINDOWS_DACL_PRIVATE
    )


def _windows_private_dacl_verdict(sddl: str, sid: str, *, directory: bool) -> str:
    """Classify one DACL as private, private-but-inheriting, or unsafe.

    The middle verdict is the one that earns its keep. A directory whose ACEs
    name exactly the private trustee set but arrive by inheritance -- no `P`,
    every ACE flagged `ID` -- has never admitted anybody outside that set, so
    nothing can hold a handle to it that this module would refuse to grant. It
    is private in the sense that matters and wrong only in that a later change
    to its parent would flow into it. That is repairable, and repairing it is
    strictly a tightening; refusing it outright is not, because the entry is
    never repaired and the caller is told to run `icacls` by hand.

    Windows hands out that exact shape constantly: any directory created inside
    an already-private parent inherits its ACEs rather than getting its own.
    The CI runner's is `O:BA D:(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;FA;;;OW)`
    -- SYSTEM, Administrators, and an owner in that set, and nothing else.

    Anything else is still `unsafe`: a foreign trustee, a right other than full
    access, a duplicate principal, a deny ACE, a malformed field count. Those
    say a principal outside the private set may already hold access, and no
    later tightening can take back a handle it has already opened.

    Reject broad or altered ACEs; inherited file ACEs are accepted precisely.

    An `OWNER RIGHTS` (`OW`) ACE counts as the current user's grant, but only
    while the current user owns the object. That is a spelling difference, not a
    weaker rule: `OW` names whoever owns the entry, so once ownership is ours the
    ACE admits exactly the principal a literal SID ACE would, and once it is not,
    this rejects it as before.

    Refusing `OW` outright was a false negative with real cost. A directory
    carrying `D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)` is protected
    against inheritance and grants full access to precisely SYSTEM,
    Administrators and its owner -- the same three principals this module writes
    itself, with no broad trustee anywhere. Windows hands out that exact shape
    for entries beneath a user's temporary directory, so the validator failed
    closed on directories that were already private, with no repair path: the
    private DACL is applied only to an entry this process's own ``mkdir``
    created, and a pre-existing entry is validated and never repaired.
    """
    if not isinstance(sddl, str) or "D:" not in sddl:
        return _WINDOWS_DACL_UNSAFE
    dacl = sddl.split("D:", 1)[1]
    protected = dacl.startswith("P")
    inheriting = False
    aces = re.findall(r"\(([^()]*)\)", dacl)
    trustees = _windows_private_dacl_trustees(sid)
    expected = {trustee.casefold() for trustee in trustees}
    # The principal an `OW` ACE resolves to, spelled the way this module spells
    # it, so a substituted grant lands in the same slot as a literal one.
    user_principal = trustees[0].casefold()
    owner = _windows_sddl_owner(sddl)
    owner_is_current_user = _windows_owner_admits_current_user(owner, sid)
    observed: set[str] = set()
    for ace in aces:
        fields = ace.split(";")
        if len(fields) != 6:
            return _WINDOWS_DACL_UNSAFE
        kind, flags, rights, _object_guid, _inherit_object_guid, trustee = fields
        if kind != "A" or rights.casefold() not in {"fa", "0x1f01ff"}:
            return _WINDOWS_DACL_UNSAFE
        principal = trustee.casefold()
        if principal in _WINDOWS_OWNER_RIGHTS_TRUSTEES:
            # Unresolvable without the owner: an SDDL string carrying no `O:`
            # cannot show that this ACE admits us rather than somebody else.
            if not owner_is_current_user:
                return _WINDOWS_DACL_UNSAFE
            principal = user_principal
        elif principal in _windows_principal_spellings(sid):
            # An alias for the current user, most often `LA`. Unlike `OW` this
            # needs no owner check: `LA` names one fixed account, and it is only
            # a spelling of *us* when that account is the current user -- which
            # is what put it in the spelling set. Folding it onto the canonical
            # principal keeps the duplicate rule below meaningful, so `LA`
            # alongside a literal grant to the same account is still one
            # principal named twice.
            principal = user_principal
        # Resolving `OW` before this check keeps the duplicate rule meaningful:
        # `OW` alongside a literal grant to the same owner is one principal
        # named twice, which is exactly what the rule is here to reject.
        if principal not in expected or principal in observed:
            return _WINDOWS_DACL_UNSAFE
        normalized_flags = flags.casefold()
        allowed = {"oici", "oiciid"} if directory else {"", "oici", "idoici", "id"}
        if normalized_flags not in allowed:
            return _WINDOWS_DACL_UNSAFE
        if directory and normalized_flags != "oici":
            inheriting = True
        observed.add(principal)
    if observed != expected:
        return _WINDOWS_DACL_UNSAFE
    if not directory:
        return _WINDOWS_DACL_PRIVATE
    if protected and not inheriting:
        return _WINDOWS_DACL_PRIVATE
    return _WINDOWS_DACL_INHERITED


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
                observed=sddl,
                expected=_windows_private_dacl_trustees(sid),
            )
    finally:
        if opened_here:
            _windows_close_handle(handle)


#: Service names to try, in order, when resolving the runtime principal. Mirrors
#: `$script:ExomemServiceNames` in `scripts/_service-common.ps1`; `kb-mcp` is the
#: pre-rename name still registered on boxes provisioned before the rename.
_WINDOWS_SERVICE_NAMES = ("exomem", "kb-mcp")

#: Account names the SCM accepts for the built-in service principals, casefolded.
#: `LookupAccountName` resolves most of these, but `LocalSystem` -- the spelling
#: the SCM itself stores for SYSTEM -- is not an account name it knows, so the
#: fixed map is the authority for these and lookup is only the fallback.
_WINDOWS_SERVICE_ACCOUNT_SIDS = {
    "localsystem": "S-1-5-18",
    ".\\localsystem": "S-1-5-18",
    "nt authority\\system": "S-1-5-18",
    "nt authority\\localservice": "S-1-5-19",
    "nt authority\\local service": "S-1-5-19",
    "nt authority\\networkservice": "S-1-5-20",
    "nt authority\\network service": "S-1-5-20",
}


@dataclass(frozen=True)
class WindowsRuntimePrincipal:
    """The principal whose private DACL the machine-local state root should carry.

    Not the calling token. The private-DACL model is token-relative, so a state
    root written by an operator's offline migration is `unsafe` to the
    LocalSystem service that then has to read it -- which is manifestation 1 of
    #933, and cost a day of toggling ACLs back and forth. The runtime principal
    is whoever will actually RUN against this root, and it is a fact about the
    machine, not about whoever happens to be typing.

    Resolved from the SCM registry, the same hive and the same non-elevated read
    `scripts/_service-common.ps1` already documents as "the single source of
    truth" for the service's interpreter. `ObjectName` sits beside the
    `Parameters\\Application` value it reads.

    With no service installed the runtime principal IS the current token, and
    every path stays byte-identical to the single-principal behaviour. An
    unreadable or unrecognised entry degrades to the current token and SAYS so
    through `source`: a guessed principal would trade one cross-principal
    failure for another, silently.
    """

    sid: str
    #: `service:<name>` when the SCM answered; `current-token` when no service is
    #: installed; `current-token (<why>)` when a service exists but its principal
    #: could not be established. Never omitted -- it is what makes a degraded
    #: resolution visible instead of indistinguishable from a clean one.
    source: str
    #: The vault this principal's service is configured to serve, when one could
    #: be established. `None` for a pinned or current-token principal, and also
    #: when the service exists but its binding is unreadable -- callers must read
    #: that as "do not act", never as "no binding exists".
    bound_vault: str | None = None

    @property
    def authoritative(self) -> bool:
        """True only for a principal established from an authority, not inferred.

        Gates the seal. A principal that merely defaulted to the current token --
        including one that defaulted BECAUSE the registry was unreadable -- must
        never authorise re-ACLing anything, or an unreadable hive would quietly
        rewrite the root to the wrong principal.

        Named for what it asserts. It was `resolved_from_service`, which was
        untrue for the `pinned:` case it also admits.
        """
        return self.source.startswith(("service:", "pinned:"))

    def seals_vault(self, vault_root: str | os.PathLike[str]) -> bool:
        """Whether this principal may seal *vault_root*'s state root.

        The seal's whole justification is a user-token flow preparing the root
        THE SERVICE WILL RUN AGAINST. A machine that merely has a service
        registered says nothing about an unrelated vault, and sealing on that
        basis handed a brand-new `exomem init` vault to LocalSystem and locked
        the operator out of the state root it had just made for them.

        An explicitly pinned principal is an operator instruction about this
        invocation, so it needs no binding. A service principal needs one, and
        an unreadable binding refuses.
        """
        if not self.authoritative:
            return False
        if self.source.startswith("pinned:"):
            return True
        return self.bound_vault is not None and _same_vault(self.bound_vault, vault_root)


#: `ERROR_FILE_NOT_FOUND` / `ERROR_PATH_NOT_FOUND`. The registry raises these for
#: a key that is not there, which is the ordinary "no service installed" answer.
#: Every other error means the read FAILED, which is a different fact.
_WINDOWS_REGISTRY_ABSENT = frozenset({2, 3})


def _windows_service_object_name(name: str) -> str | None:
    """Read one service's configured account from the SCM hive.

    Returns None only when the service is genuinely ABSENT. A read that FAILED
    -- permissions, a corrupt hive, a redirected view -- raises instead, because
    the two are different facts and collapsing them is how a correctly sealed
    root got a `current-token` principal, a FAIL, and an `icacls` that would
    have granted the operator and broken the service.
    """
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            f"SYSTEM\\CurrentControlSet\\Services\\{name}",
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "ObjectName")
    except OSError as error:
        if getattr(error, "winerror", None) in _WINDOWS_REGISTRY_ABSENT:
            return None
        raise
    return value.strip() if isinstance(value, str) and value.strip() else None


def _windows_service_bound_vault(name: str) -> str | None:
    r"""The vault path one installed service is configured to serve, or None.

    Resolution mirrors `scripts/upgrade.ps1` (~:120) exactly, because the two
    have to agree about which cell is which: the managed dotenv in the service's
    NSSM `AppDirectory` first, then the `AppEnvironmentExtra` NSSM injects. The
    dotenv wins because python-dotenv loads it with `override=True` before
    readiness, so it is what the running service actually reads.

    Never raises, and never returns a value it had to guess at. `None` means
    "this service's vault could not be established", which callers must treat as
    "do not act", not as "no binding exists".
    """
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            f"SYSTEM\\CurrentControlSet\\Services\\{name}\\Parameters",
        ) as key:
            try:
                app_directory, _kind = winreg.QueryValueEx(key, "AppDirectory")
            except OSError:
                app_directory = None
            try:
                extra, _kind = winreg.QueryValueEx(key, "AppEnvironmentExtra")
            except OSError:
                extra = None
    except OSError:
        return None
    if isinstance(app_directory, str) and app_directory.strip():
        dotenv = Path(app_directory.strip()) / ".env"
        try:
            lines = dotenv.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        for line in reversed(lines):
            match = re.match(r"\s*EXOMEM_VAULT_PATH\s*=\s*(.*?)\s*$", line)
            if match:
                value = match.group(1).strip().strip("\"'")
                if value:
                    return value
    # NSSM stores this as REG_MULTI_SZ; only ever read the one key out of it.
    # The rest of that block carries service secrets and must not be surfaced.
    if isinstance(extra, list):
        for item in extra:
            if isinstance(item, str) and item.startswith("EXOMEM_VAULT_PATH="):
                value = item.split("=", 1)[1].strip().strip("\"'")
                if value:
                    return value
    return None


def _same_vault(left: str | os.PathLike[str], right: str | os.PathLike[str]) -> bool:
    """Whether two spellings name the same vault, by the state key's own rule.

    Derived from `state_paths.vault_state_key`'s normalization rather than
    restated: two paths that map to the same state key ARE the same vault by
    definition, so comparing keys cannot drift from the placement seam.
    """
    from .state_paths import vault_state_key

    try:
        return vault_state_key(Path(left)) == vault_state_key(Path(right))
    except (OSError, ValueError):
        return False


def _windows_sid_for_account(account: str) -> str | None:
    """Resolve one account name to its SID string, or None when unresolvable."""
    fixed = _WINDOWS_SERVICE_ACCOUNT_SIDS.get(account.casefold())
    if fixed is not None:
        return fixed
    import ctypes
    from ctypes import wintypes

    advapi32 = _windows_library(ctypes, "advapi32")
    lookup = advapi32.LookupAccountNameW
    lookup.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
    ]
    lookup.restype = wintypes.BOOL
    sid_size = wintypes.DWORD(0)
    domain_size = wintypes.DWORD(0)
    use = wintypes.DWORD(0)
    lookup(None, account, None, ctypes.byref(sid_size), None,
           ctypes.byref(domain_size), ctypes.byref(use))
    if not sid_size.value:
        return None
    sid_buffer = ctypes.create_string_buffer(sid_size.value)
    domain_buffer = ctypes.create_unicode_buffer(domain_size.value)
    if not lookup(None, account, sid_buffer, ctypes.byref(sid_size), domain_buffer,
                  ctypes.byref(domain_size), ctypes.byref(use)):
        return None
    convert = advapi32.ConvertSidToStringSidW
    convert.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    convert.restype = wintypes.BOOL
    text = wintypes.LPWSTR()
    if not convert(sid_buffer, ctypes.byref(text)):
        return None
    try:
        return str(text.value)
    finally:
        _windows_library(ctypes, "kernel32").LocalFree(text)


#: Explicit override for the runtime principal. `current-token` pins the
#: single-principal behaviour; anything else must be a literal SID. Two callers
#: need it and neither is exotic: an operator doing maintenance inside a stop
#: window with the service disabled, and the test suite -- which must not change
#: behaviour based on whether the developer's machine happens to have an
#: `exomem` service registered, since that would make the suite's verdict a
#: property of the box rather than of the code.
ENV_RUNTIME_PRINCIPAL = "EXOMEM_RUNTIME_PRINCIPAL"


def resolve_windows_runtime_principal(
    *, service_names: tuple[str, ...] = _WINDOWS_SERVICE_NAMES
) -> WindowsRuntimePrincipal:
    """Whose private DACL the state root should carry on this machine.

    Never guesses. Every failure to establish a SERVICE principal degrades to
    the current token with the reason recorded in `source`, because a wrong
    principal is worse than a conservative one: it would lock out the operator
    AND fail the service.

    It can still raise `OSError` from `_windows_current_user_sid`, which is the
    one question with no honest fallback -- a process that cannot establish its
    own identity has nothing to degrade TO. Callers that must not fail catch it.
    """
    current = _windows_current_user_sid()
    override = os.environ.get(ENV_RUNTIME_PRINCIPAL, "").strip()
    if override:
        if override.casefold() == "current-token":
            # `pinned:` and NOT `current-token (...)`: the parenthesised form is
            # reserved for a principal that DEGRADED to this token, and callers
            # withhold the token-relative repair on seeing it. A deliberate pin
            # is the opposite of a degradation and must not read as one.
            return WindowsRuntimePrincipal(sid=current, source="pinned:current-token")
        if re.fullmatch(r"S-1-[0-9-]+", override):
            return WindowsRuntimePrincipal(sid=override, source=f"pinned:{override}")
        return WindowsRuntimePrincipal(
            sid=current,
            source=f"current-token ({ENV_RUNTIME_PRINCIPAL}={override!r} is not a SID)",
        )
    degraded: str | None = None
    for name in service_names:
        try:
            account = _windows_service_object_name(name)
        except OSError as error:
            # A read that FAILED, not an absent service. Degrading silently here
            # made a correctly-sealed cell look misconfigured and prescribed the
            # repair that breaks it.
            degraded = f"service {name!r} registry read failed ({error})"
            continue
        if account is None:
            continue
        try:
            sid = _windows_sid_for_account(account)
        except OSError:
            sid = None
        if sid is None:
            degraded = f"service {name!r} account {account!r} is unresolvable"
            continue
        return WindowsRuntimePrincipal(
            sid=sid,
            source=f"service:{name}",
            bound_vault=_windows_service_bound_vault(name),
        )
    if degraded is not None:
        return WindowsRuntimePrincipal(sid=current, source=f"current-token ({degraded})")
    return WindowsRuntimePrincipal(sid=current, source="current-token")


@dataclass(frozen=True)
class WindowsDirectoryPosture:
    """What the CURRENT process token can do with one private directory.

    The private-DACL model is relative to the calling token
    (`_windows_private_dacl_trustees` puts that token's own SID first), so the
    same directory is `private` to the principal that created it and `unsafe`
    to any other. That is correct for a single-principal install and is exactly
    the cross-principal defect when a LocalSystem service and an operator's CLI
    share one machine-local state root: whichever principal did not create it
    fails, and until now it failed as a traceback from deep inside whatever
    operation happened to touch state first.

    This type is the read-only vocabulary for reporting that condition instead
    of raising it. It never repairs and never opens anything for write.

    It carries TWO verdicts because the two questions operators actually ask are
    different. "Can I work here?" is about the calling token. "Is this cell
    configured correctly?" is about the RUNTIME principal -- and answering the
    second with the first makes a healthy LocalSystem install permanently red to
    every operator, since a correct `D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)` root is
    `unsafe` to anyone who is not SYSTEM. Measured, not assumed.
    """

    path: Path
    #: This token can open the directory itself.
    accessible: bool
    #: Why the open failed, when it did: `access-denied` (the cross-principal
    #: case), `reparse-point`, `unexpected-path-type`, `missing`, `unopenable`.
    #: None when it succeeded. Collapsing these lost the difference between "a
    #: principal denied me" and "this is a junction" -- and only the first has an
    #: `icacls` repair.
    unopenable_reason: str | None
    #: `private` / `inherited` / `unsafe` as THIS TOKEN's validator judges it,
    #: or None when the descriptor could not be read at all.
    verdict: str | None
    #: The same judgement made for the runtime principal. Equal to `verdict`
    #: whenever they are the same principal.
    runtime_verdict: str | None
    #: The worst verdict among a sample of the root's CHILDREN, judged for the
    #: runtime principal, or None when there was nothing readable to judge. The
    #: root's own descriptor cannot answer whether the state inside it is
    #: private, and a sealed-looking root over readable state is worse than an
    #: honestly unsealed one.
    child_verdict: str | None
    #: How many children that verdict is based on. 0 with a None verdict means
    #: the contents were NOT EVALUATED, which callers must say out loud rather
    #: than report as privacy.
    child_sampled: int
    #: The observed SDDL, when readable. An object's OWNER keeps READ_CONTROL,
    #: so a root this user created but re-ACLed to SY/BA still reports its
    #: descriptor -- which is what makes a remote report actionable.
    observed: str | None
    #: The full-access trustees THIS TOKEN's validator requires.
    expected: tuple[str, ...]
    #: The full-access trustees the RUNTIME principal's validator requires.
    runtime_expected: tuple[str, ...]
    #: The runtime principal, and how it was established.
    runtime_principal: WindowsRuntimePrincipal
    #: The exact non-recursive repair, rendered for the RUNTIME principal -- the
    #: DACL this root is supposed to end up carrying. Rendering it for the
    #: calling token instead is what produced #933's day of toggling: following
    #: it on a service install writes a root the service then rejects forever.
    remediation: str


def inspect_windows_private_directory(
    path: Path, *, runtime_principal: WindowsRuntimePrincipal | None = None
) -> WindowsDirectoryPosture | None:
    """Describe one private directory's posture. Read-only; does not repair.

    Returns None off Windows (the POSIX branch has no DACL model) and None when
    the entry is absent -- an absent root is not a cross-principal root, and
    conflating them would make every fresh install look like the defect.

    Raises only if the CURRENT TOKEN's own identity cannot be established
    (`_windows_current_user_sid`, or an invalid SID reaching
    `_windows_private_dacl_trustees`). Everything about the directory itself is
    reported rather than raised; it is only the question "who am I" that has no
    honest answer to report.
    """
    if os.name != "nt":
        return None
    target = Path(path)
    if not os.path.lexists(target):
        return None
    sid = _windows_current_user_sid()
    principal = runtime_principal or resolve_windows_runtime_principal()
    expected = _windows_private_dacl_trustees(sid)
    runtime_expected = _windows_private_dacl_trustees(principal.sid)
    remediation = _windows_private_dacl_repair_command(
        target, principal.sid, directory=True
    )
    # Read the descriptor by path first: it survives the loss of directory
    # access that makes the handle open fail, so the two probes answer
    # different questions and the descriptor one must not be skipped when the
    # other fails.
    try:
        observed: str | None = _windows_dacl_sddl(target)
    except OSError:
        observed = None

    def _verdict(for_sid: str) -> str | None:
        if observed is None:
            return None
        return _windows_private_dacl_verdict(observed, for_sid, directory=True)

    accessible = True
    unopenable_reason: str | None = None
    try:
        handle = _windows_open_path(target, directory=True)
    except WindowsReparsePointError:
        accessible, unopenable_reason = False, "reparse-point"
    except WindowsPathTypeError:
        accessible, unopenable_reason = False, "unexpected-path-type"
    except FileNotFoundError:
        accessible, unopenable_reason = False, "missing"
    except OSError as error:
        accessible = False
        # `_windows_open_path` passes the raw Windows error through as `errno`,
        # so ACCESS_DENIED arrives as 5 rather than as EACCES.
        unopenable_reason = (
            "access-denied" if error.errno in {5, errno.EACCES} else "unopenable"
        )
    else:
        _windows_close_handle(handle)
    child_verdict, child_sampled = _windows_child_dacl_verdict(target, principal.sid)
    return WindowsDirectoryPosture(
        path=target,
        accessible=accessible,
        unopenable_reason=unopenable_reason,
        verdict=_verdict(sid),
        runtime_verdict=_verdict(principal.sid),
        child_verdict=child_verdict,
        child_sampled=child_sampled,
        observed=observed,
        expected=expected,
        runtime_expected=runtime_expected,
        runtime_principal=principal,
        remediation=remediation,
    )


#: How many children to sample when asking whether the state INSIDE a private
#: root is private too. A sample, not a sweep: this runs inside `doctor`, the
#: root can hold thousands of index pages, and one foreign ACE is as damning as
#: a hundred. `sorted` so the sample is the same set run to run rather than
#: whatever order the filesystem happened to hand back.
_WINDOWS_CHILD_DACL_SAMPLE = 16


def _windows_sample_children(directory: Path) -> list[Path] | None:
    """A stable sample of one directory's children, or None when unlistable.

    `sorted` so the sample is the same set run to run rather than whatever order
    the filesystem happened to hand back. None and [] are different answers:
    None means the listing was refused, [] means there is genuinely nothing
    there, and only the second licenses a claim about the contents.
    """
    if os.name != "nt":
        return None
    try:
        return sorted(Path(directory).iterdir())[:_WINDOWS_CHILD_DACL_SAMPLE]
    except OSError:
        return None


def _windows_child_dacl_verdict(
    directory: Path, sid: str, *, children: list[Path] | None = None
) -> tuple[str | None, int]:
    """Worst verdict among a sample of the directory's children, and its size.

    The root's own descriptor cannot answer whether the STATE is private.
    Protecting a directory converts its children's inherited ACEs to explicit
    ones, so a root that reports `private` can sit over files any other
    principal still reads and writes -- which is the exact hazard this module's
    placement docstring opens with, and it read as `pass`.

    The count is returned, not implied. A `None` verdict with 0 sampled means
    the contents were NOT EVALUATED -- the normal case for an operator looking
    at a service-owned root, whose listing is denied -- and a caller that
    reports privacy without saying so is making exactly the confidently-green
    claim this check exists to stop.

    `children` lets a caller supply paths it enumerated earlier. The seal needs
    that: after it writes, its own listing is denied, but child DESCRIPTORS stay
    readable by path because the owner keeps `READ_CONTROL`.
    """
    if os.name != "nt":
        return (None, 0)
    sample = children if children is not None else _windows_sample_children(directory)
    if sample is None:
        return (None, 0)
    seen: list[str] = []
    for child in sample:
        try:
            child_sddl = _windows_dacl_sddl(child)
            # Inside the guard: `Path.is_dir` SWALLOWS `OSError`, so a denied
            # child directory would silently be judged with the file flag-set.
            is_directory = child.is_dir()
        except OSError:
            continue
        seen.append(_windows_private_dacl_verdict(child_sddl, sid, directory=is_directory))
    if not seen:
        return (None, 0)
    # One rule, ordered worst-first. This was a short-circuit on `unsafe` plus an
    # accumulator, and the accumulator only ever escalated to `inherited` -- so
    # with the short-circuit removed an `unsafe` child that did not sort first
    # was silently dropped. A single ranking cannot develop that seam.
    for verdict in (
        _WINDOWS_DACL_UNSAFE,
        _WINDOWS_DACL_INHERITED,
        _WINDOWS_DACL_PRIVATE,
    ):
        if verdict in seen:
            return (verdict, len(seen))
    return (seen[0], len(seen))


class WindowsRuntimePrincipalUnresolved(RuntimeError):
    """The runtime principal could not be established, so nothing was re-ACLed.

    Deliberately not a silent no-op. Sealing to a GUESSED principal is the one
    outcome worse than not sealing: it locks the operator out AND leaves the
    service failing closed, which is the pair of failures #933 is about.
    """


def seal_windows_state_root_for_runtime_principal(
    state_dir: Path,
    *,
    vault_root: Path | None = None,
    runtime_principal: WindowsRuntimePrincipal | None = None,
) -> WindowsRuntimePrincipal | None:
    """Leave one state root carrying the RUNTIME principal's private DACL.

    Called at the END of a user-token flow that created or recreated the root,
    never at creation: the creating token has to be able to write the state into
    it first, and a root sealed to LocalSystem up front is one its own migrator
    can no longer open.

    Returns the principal it sealed for, or None when there was nothing to do:
    POSIX, an absent root, a runtime principal that IS the current token, or --
    the case that matters -- a vault this machine's service is not bound to.
    That last one is not a nicety. Resolving a principal from the machine's
    service registry says nothing about WHICH vault that service serves, so
    without the binding check an `exomem init` of a brand-new unrelated vault
    handed its state root to LocalSystem and locked the operator out of the
    directory it had just made for them.

    Refuses rather than guessing when the principal is unresolved, refuses to
    touch a root this process does not own, and refuses an aliased root: the
    seal is the one DACL write that never opened a handle, so without its own
    check it would happily protect a junction and leave the real root untouched
    while reporting success.
    """
    if os.name != "nt":
        return None
    target = Path(state_dir)
    if not os.path.lexists(target):
        return None
    principal = runtime_principal or resolve_windows_runtime_principal()
    current = _windows_current_user_sid()
    if principal.sid.casefold() == current.casefold():
        return None
    if not principal.authoritative:
        raise WindowsRuntimePrincipalUnresolved(principal.source)
    if vault_root is not None and not principal.seals_vault(vault_root):
        # Two very different skips, and they must not look alike in a log. One
        # is correct and expected; the other is a SILENT MISS on the upgrade
        # path -- the service's own vault going unsealed because its binding
        # could not be read.
        if principal.bound_vault is None:
            logger.warning(
                "not sealing %s: the %s binding could not be read, so this vault "
                "cannot be confirmed as the one it serves",
                target, principal.source,
            )
        else:
            logger.info(
                "not sealing %s: %s is bound to a different vault (%s)",
                target, principal.source, principal.bound_vault,
            )
        return None
    # Open it the way every other private-state boundary does, so a reparse
    # point or a non-directory is refused here too rather than silently sealed.
    try:
        _windows_close_handle(_windows_open_path(target, directory=True))
    except (WindowsReparsePointError, WindowsPathTypeError):
        raise
    except OSError:
        # Unopenable. If it is ALREADY exactly what this call would write, that
        # is a repeat call on a sealed root, and a seal has to be idempotent:
        # the first version raised a raw `[Errno 5]` from here on the second
        # call. Anything else is a real failure and still propagates.
        try:
            already = _windows_private_dacl_is_valid(
                _windows_dacl_sddl(target), principal.sid, directory=True
            )
        except OSError:
            already = False
        if already:
            return None
        raise
    observed = _windows_dacl_sddl(target)
    if not _windows_owner_admits_current_user(_windows_sddl_owner(observed), current):
        raise WindowsRuntimePrincipalUnresolved(
            f"this process does not own {target}; refusing to re-ACL it"
        )
    # Enumerate BEFORE the write, while the listing is still permitted. After
    # the seal this token cannot list the root, but child descriptors stay
    # readable by path because the owner keeps `READ_CONTROL` -- so this is the
    # only moment at which the contents can be named for later verification.
    children = _windows_sample_children(target)
    # `propagate` is load-bearing, not a flag. Protecting the entry alone makes
    # Windows convert each child's inherited ACEs into explicit ones, so every
    # file the migration just moved in stays readable and writable by this token
    # behind a directory that merely LOOKS sealed.
    _windows_apply_private_dacl(target, principal.sid, propagate=True)
    # Prove the write took, judged by the same validator the runtime principal
    # will use. Read by path: the seal has just removed this token's own access,
    # so a fresh handle open would fail on a root that is now exactly correct.
    sealed = _windows_dacl_sddl(target)
    if not _windows_private_dacl_is_valid(sealed, principal.sid, directory=True):
        raise WindowsRuntimeDaclError(
            target,
            _windows_private_dacl_repair_command(target, principal.sid, directory=True),
            observed=sealed,
            expected=_windows_private_dacl_trustees(principal.sid),
        )
    # And prove it took on the CONTENTS, which is what the seal actually claims
    # to have fixed. Checking only the entry would report success over a partial
    # propagation -- a child directory carrying its own protected DACL stays
    # `unsafe` for the runtime principal and inheritance never reaches it.
    child_verdict, _sampled = _windows_child_dacl_verdict(
        target, principal.sid, children=children
    )
    if child_verdict == _WINDOWS_DACL_UNSAFE:
        raise WindowsRuntimeDaclError(
            target,
            _windows_private_dacl_repair_command(target, principal.sid, directory=True),
            observed=f"root sealed, but contents remain {child_verdict}",
            expected=_windows_private_dacl_trustees(principal.sid),
        )
    return principal


def prepare_windows_idempotency_runtime_paths(state_dir: Path, owners_dir: Path) -> None:
    """Create the two runtime directories with protected DACLs, never repair old ones."""
    if os.name != "nt":
        return
    prepare_windows_private_state_root(state_dir)
    _prepare_windows_private_directory(owners_dir)
    sid = _windows_current_user_sid()
    state = _acquire_secure_directory(state_dir, create=False)
    try:
        _validate_windows_runtime_entry(
            state_dir, directory=True, sid=sid, handle=state.windows_handle
        )
        owners = _acquire_secure_directory(owners_dir, create=False)
        try:
            if not _windows_child_is_in_directory(state, owners.windows_handle):
                raise OSError("Windows owner directory escaped its retained state directory")
            _validate_windows_runtime_entry(
                owners_dir, directory=True, sid=sid, handle=owners.windows_handle
            )
        finally:
            owners.close()
    finally:
        state.close()


def prepare_windows_private_state_root(state_dir: Path) -> None:
    """Establish or validate the private Windows root before any lock artifact."""
    if os.name != "nt":
        return
    _prepare_windows_private_directory(Path(state_dir))


def _prepare_windows_private_directory(path: Path) -> None:
    """Create exactly one private directory or validate an observed entry.

    A process sets permissions only on the exact entry its own ``mkdir``
    created.  A concurrent loser waits briefly for that creator to finish its
    DACL write, then validates without repairing the observed entry.

    Missing *ancestors* are created first, without a private DACL.  That is not
    a relaxation: the POSIX branch this mirrors passes ``parents=True``, and
    ``Path.mkdir`` applies its ``mode`` only to the leaf there too, so the
    ancestors are ordinary directories on both platforms and the privacy
    guarantee sits on ``target`` alone.  Without this, a Windows profile whose
    ``~/.cache`` does not exist -- which is every fresh Windows profile, since
    ``.cache`` is an XDG convention Windows does not create -- cannot construct
    the idempotency store at all, and the server dies at startup with
    ``WinError 3``.  There is a Windows lane now (``cross-platform.yml``), but it
    is advisory and does not gate a merge, so this still has to be reasoned about
    rather than left to the suite.
    """
    target = Path(path).expanduser().absolute()
    created = False
    created_identity: os.stat_result | None = None
    try:
        directory = _acquire_secure_directory(target, create=False)
    except FileNotFoundError:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            created = True
            created_identity = os.lstat(target)
            _after_windows_secure_directory_create(target)
        directory = _acquire_secure_directory(target, create=False)
    sid = _windows_current_user_sid()
    try:
        if created:
            if (
                created_identity is None
                or not os.path.samestat(created_identity, os.lstat(target))
                or not _same_directory_path(directory)
            ):
                raise OSError("Windows directory changed during creation")
            _windows_apply_private_dacl(target, sid)
            _validate_windows_runtime_entry(
                target, directory=True, sid=sid, handle=directory.windows_handle
            )
            return
        deadline = time.monotonic() + _WINDOWS_DACL_STABILIZATION_SECONDS
        tightened = False
        while True:
            try:
                _validate_windows_runtime_entry(
                    target, directory=True, sid=sid, handle=directory.windows_handle
                )
                return
            except WindowsRuntimeDaclError:
                observed = _windows_dacl_sddl_for_handle(directory.windows_handle)
                inherited = (
                    _windows_private_dacl_verdict(observed, sid, directory=True)
                    == _WINDOWS_DACL_INHERITED
                )
                # Once. A second attempt would mean the write did not take, and
                # looping on that until the deadline turns a permission problem
                # into a stall rather than the error it is.
                if inherited and not tightened:
                    tightened = True
                    _windows_tighten_private_directory(target, directory, sid)
                    continue
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_WINDOWS_DACL_STABILIZATION_POLL_SECONDS)
    finally:
        directory.close()


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


@contextlib.contextmanager
def _open_windows_directory_for_flush(path: Path) -> Iterator[int]:
    """Retain one native directory leaf suitable for a durability flush."""
    expected = os.lstat(path)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(expected.st_mode)
        or not stat.S_ISDIR(expected.st_mode)
        or getattr(expected, "st_file_attributes", 0) & reparse_point
    ):
        raise OSError("Windows durability directory is not a real directory")
    with contextlib.ExitStack() as stack:
        parent = stack.enter_context(_open_secure_directory(path.parent, create=False))
        handle = _windows_open_path(
            path,
            directory=True,
            access=0x40000000,  # GENERIC_WRITE only on the exact final directory
            share=0x3,  # FILE_SHARE_READ | FILE_SHARE_WRITE; never FILE_SHARE_DELETE
        )
        stack.callback(_windows_close_handle, handle)
        if not _windows_child_is_in_directory(parent, handle):
            raise OSError("Windows durability directory escaped its retained parent")
        _windows_handle_identity(handle)
        if not os.path.samestat(expected, os.lstat(path)):
            raise OSError("Windows durability directory changed during open")
        yield handle


def _windows_flush_directory(path: Path) -> None:
    """Flush one no-follow Windows directory entry with exact handle ownership."""
    with _open_windows_directory_for_flush(path) as handle:
        _windows_flush_directory_handle(handle)


def _windows_flush_directory_handle(handle: int) -> None:
    """Flush one raw native directory handle without taking ownership of it."""
    import ctypes
    from ctypes import wintypes

    kernel32 = _windows_library(ctypes, "kernel32")
    flush = kernel32.FlushFileBuffers
    flush.argtypes = [wintypes.HANDLE]
    flush.restype = wintypes.BOOL
    if not flush(handle):
        raise OSError(_windows_last_error(ctypes), "cannot flush Windows directory")


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
                created = os.lstat(current)
                _after_windows_secure_directory_create(current)
                if not os.path.samestat(created, os.lstat(current)):
                    raise OSError("Windows directory changed during creation") from None
                handles.append(open_path(current, directory=True))
                if not os.path.samestat(created, os.lstat(current)):
                    raise OSError("Windows directory changed during creation") from None
        return _SecureDirectory(
            absolute, windows_handles=handles, close_windows_handle=close_handle
        )
    except BaseException:
        while handles:
            close_handle(handles.pop())
        raise


def _after_windows_secure_directory_create(_path: Path) -> None:
    """Test seam between native directory creation and retained-handle open."""


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
                getattr(msvcrt, "get_osfhandle")(fd)  # noqa: B009 - Windows compatibility seam
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


def _windows_create_child_directory_handle(parent: _SecureDirectory, name: str) -> int:
    """Atomically create a direct directory child and return its native handle."""
    import ctypes
    from ctypes import wintypes

    class _UnicodeString(ctypes.Structure):
        _fields_ = [("length", wintypes.USHORT), ("maximum", wintypes.USHORT), ("buffer", wintypes.LPWSTR)]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.ULONG), ("root", wintypes.HANDLE), ("name", ctypes.POINTER(_UnicodeString)),
            ("attributes", wintypes.ULONG), ("security", wintypes.LPVOID), ("quality", wintypes.LPVOID),
        ]

    class _IoStatusBlock(ctypes.Structure):
        _fields_ = [("status", ctypes.c_long), ("information", ctypes.c_size_t)]

    value = ctypes.create_unicode_buffer(name)
    encoded_len = len(name.encode("utf-16-le"))
    # `create_unicode_buffer` returns a `c_wchar_Array`, and `buffer` is declared
    # `LPWSTR`; ctypes does not convert one to the other in a structure
    # initialiser, it raises `TypeError: incompatible types, c_wchar_Array_N
    # instance instead of c_wchar_p instance`. So this call has never reached
    # `NtCreateFile` on Windows. `value` stays referenced for the duration of
    # the call, which is what keeps the cast pointer valid.
    unicode_name = _UnicodeString(
        encoded_len, encoded_len + 2, ctypes.cast(value, wintypes.LPWSTR)
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes), parent.windows_handle, ctypes.pointer(unicode_name), 0x40, None, None
    )
    result = wintypes.HANDLE()
    status = _IoStatusBlock()
    ntdll = _windows_library(ctypes, "ntdll")
    create = ntdll.NtCreateFile
    create.argtypes = [
        ctypes.POINTER(wintypes.HANDLE), wintypes.ULONG, ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock), wintypes.LPVOID, wintypes.ULONG, wintypes.ULONG,
        wintypes.ULONG, wintypes.ULONG, wintypes.LPVOID, wintypes.ULONG,
    ]
    create.restype = ctypes.c_long
    code = create(
        ctypes.byref(result), 0x00130080, ctypes.byref(attributes), ctypes.byref(status), None,
        0, 0x7, 2, 0x00200021, None, 0,
    )
    if code < 0:
        raise OSError(int(code), "NtCreateFile directory creation refused")
    return int(cast(int, result.value))


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
    handle = _windows_create_child_directory_handle(parent, name)
    child = _SecureDirectory(parent.path / name, windows_handles=[handle])
    try:
        if not _same_directory_path(parent) or not _windows_child_is_in_directory(parent, child.windows_handle):
            raise OSError("Windows child directory escaped its retained parent")
        return child
    except BaseException:
        child.close()
        raise


def retain_child_directory(
    parent: _SecureDirectory, name: str, *, delete_access: bool = False
) -> _SecureDirectory:
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
    if delete_access:
        handle = _windows_open_path(parent.path / name, directory=True, access=0x00030080)
        child = _SecureDirectory(parent.path / name, windows_handles=[handle])
    else:
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
    staging_path = directory.path / f".{name}.new"
    if staging_path.exists():
        raise FileExistsError("retained Windows staging entry exists")
    fd = _open_secure_file_at(
        directory, staging_path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL, delete_access=True
    )
    try:
        os.write(fd, content)
        os.fsync(fd)
        import msvcrt

        source_handle = msvcrt.get_osfhandle(fd)
        source_identity = _windows_handle_identity(source_handle)
        if not replace:
            try:
                existing = _windows_open_path(target, directory=False)
            except FileNotFoundError:
                pass
            else:
                _windows_close_handle(existing)
                raise FileExistsError("retained Windows destination exists")
        _windows_rename_handle(source_handle, directory, name, replace=replace)
        published = _windows_open_path(target, directory=False)
        try:
            if _windows_handle_identity(published) != source_identity:
                raise OSError("retained Windows manifest identity changed")
        finally:
            _windows_close_handle(published)
        if not _same_directory_path(directory):
            raise OSError("retained Windows directory changed during file publish")
    finally:
        os.close(fd)


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
    held = retain_regular_child_file(directory, name)
    try:
        if os.name != "nt":
            assert directory.fd is not None
            os.unlink(name, dir_fd=directory.fd)
            os.fsync(directory.fd)
        else:
            import msvcrt

            _windows_delete_handle(msvcrt.get_osfhandle(held.fd))
            if not _same_directory_path(directory):
                raise OSError("retained Windows directory changed during unlink")
    finally:
        held.close()


def remove_retained_child_directory(
    parent: _SecureDirectory, child: _SecureDirectory, name: str
) -> None:
    """Remove exactly one proven empty child directory without a path reopen gap."""
    if not _same_directory_path(parent):
        raise OSError("retained directory relationship changed")
    if os.name == "nt" and not _windows_child_is_in_directory(parent, child.windows_handle):
        raise OSError("retained Windows directory escaped parent")
    if os.name != "nt":
        assert parent.fd is not None
        child.close()
        os.rmdir(name, dir_fd=parent.fd)
        os.fsync(parent.fd)
        return
    _windows_delete_handle(child.windows_handle)
    child.close()
    try:
        retain_child_directory(parent, name)
    except FileNotFoundError:
        return
    raise OSError("retained Windows directory removal did not complete")


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
            held = retain_regular_child_file(directory, name)
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
        if flags & os.O_CREAT and flags & os.O_EXCL:
            creation = 1  # CREATE_NEW
        else:
            creation = 4 if flags & os.O_CREAT else 3  # OPEN_ALWAYS / OPEN_EXISTING
        handle = _windows_open_path(
            directory.path / name,
            directory=False,
            access=access,
            share=0x7 if delete_access else 0x3,
            creation=creation,
        )
        try:
            if not _windows_child_is_in_directory(directory, handle):
                raise OSError("Windows runtime file escaped its retained directory")
            fd = getattr(msvcrt, "open_osfhandle")(handle, flags | getattr(os, "O_BINARY", 0))  # noqa: B009 - Windows compatibility seam
        except BaseException:
            _windows_close_handle(handle)
            raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("lock path is not a regular file")
    except BaseException:
        os.close(fd)
        raise
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
    # Contention attribution.  These are *measurement only* — nothing in the
    # acquire path reads them, so no fairness or timeout behaviour depends on
    # them.  They are deliberately maintained without taking any new lock:
    # `int` increments and `deque.append` are cheap, and losing a count under
    # extreme thread contention degrades a diagnostic rather than a decision.
    # They are also strictly PROCESS-LOCAL: a second exomem process holding the
    # same OS boundary contributes nothing here, which is why the published
    # block carries `scope: "process_local"`.
    acquire_attempts: int = 0
    busy_refusals: int = 0
    # MONOTONIC refusal timestamps, deliberately not wall-clock: the recency
    # window is elapsed-time arithmetic, and an NTP step (or a DST/manual clock
    # change) would otherwise silently drop or invent recent refusals during
    # exactly the contention incident this window exists to diagnose.  Wall
    # clock is kept only for the displayed `observed_at`.
    busy_refusal_monotonic: deque[float] = field(
        default_factory=lambda: deque(maxlen=_CONTENTION_RECENT_SAMPLES)
    )
    last_holder: dict[str, object] | None = None


_LOCAL_STATES: dict[Path, _LocalLockState] = {}
_LOCAL_STATES_GUARD = threading.Lock()


def _note_acquire_attempt(state: _LocalLockState) -> None:
    """Count one `hold()` entry, including re-entrant ones (they never contend).

    No lock: this is a diagnostic counter, and taking one would add an
    acquisition to the hot path the counter exists to measure.
    """
    state.acquire_attempts += 1


def _note_busy_refusal(
    state: _LocalLockState, snapshot: Mapping[str, object] | None
) -> None:
    """Record one MUTATION_BUSY refusal and whatever holder it could observe.

    A refusal that observes no holder (the starvation case: the boundary was
    free at the instant of the probe, yet the bounded acquire still lost) still
    increments the counters; `last_holder` keeps the previous observation
    because it is the *last known* holder, not the currently-observed one.
    """
    state.busy_refusals += 1
    # Window arithmetic on the monotonic clock; display timestamp on the wall
    # clock.  They are not interchangeable and must not be mixed.
    state.busy_refusal_monotonic.append(time.monotonic())
    if snapshot is not None and snapshot.get("state") == "held":
        state.last_holder = {
            # A refusal can observe another process's holder, whose pid this
            # process never learns; the sidecar record is content-free labels.
            "pid": None,
            "request_id": _safe_label(snapshot.get("request_id"), fallback="untracked"),
            "operation": _safe_label(snapshot.get("operation"), fallback="unknown"),
            "holder_kind": _safe_label(snapshot.get("holder_kind"), fallback="unknown"),
            "observed_at": time.time(),
            "source": "refusal",
        }


def _note_release(
    state: _LocalLockState,
    *,
    request_id: str | None,
    operation: str | None,
    holder_kind: str | None,
) -> None:
    """Record the hold this process just released as the last-known holder."""
    state.last_holder = {
        "pid": os.getpid(),
        "request_id": _safe_label(request_id, fallback="untracked"),
        "operation": _safe_label(operation, fallback="unknown"),
        "holder_kind": _safe_label(holder_kind, fallback="unknown"),
        "observed_at": time.time(),
        "source": "release",
    }


def _contention_view(state: _LocalLockState) -> dict[str, object]:
    """Project the process-local contention counters into a content-free block."""
    horizon = time.monotonic() - _CONTENTION_RECENT_WINDOW_SECONDS
    recent = sum(1 for at in tuple(state.busy_refusal_monotonic) if at >= horizon)
    last_holder = state.last_holder
    return {
        "acquire_attempts": state.acquire_attempts,
        "busy_refusals": state.busy_refusals,
        "busy_refusals_recent": recent,
        "recent_window_seconds": _CONTENTION_RECENT_WINDOW_SECONDS,
        # Cross-process caveat: these counts describe this process only.  A
        # concurrent writer in another process is invisible here, so a zero
        # refusal count is not evidence that the boundary is uncontended.
        "scope": "process_local",
        "last_holder": dict(last_holder) if last_holder is not None else None,
    }


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
        publish_holder_metadata: bool = True,
    ) -> Iterator[None]:
        """Hold both the local and OS mutation guards for the bounded interval.

        Synthetic reserved-state locks may omit the diagnostic holder sidecar:
        their high-frequency filesystem-identity coordination needs the OS lock,
        not a durable attribution record for every short hold. Command and
        lifecycle mutation boundaries remain attributable.
        """
        if type(publish_holder_metadata) is not bool:
            raise TypeError("holder metadata publication flag must be boolean")
        if not publish_holder_metadata and holder_kind != "reserved-state":
            raise ValueError(
                "metadata-free holds are limited to reserved-state coordination"
            )
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout < 0:
            raise ValueError("mutation lock timeout must be non-negative")
        wait_start = time.monotonic()
        # Clear any prior hold's timing so a mutation that fails BEFORE
        # acquiring can never journal a previous mutation's wait/hold values.
        _LAST_MUTATION_TIMING.set(None)
        deadline = wait_start + timeout
        state = _state_for(self.lock_path)
        _note_acquire_attempt(state)
        remaining = max(0.0, deadline - time.monotonic())
        if not state.guard.acquire(timeout=remaining):
            raise self._refused(wait_start)
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
                    publish_holder_metadata=publish_holder_metadata,
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
                    # Read the labels under the guard this path already holds;
                    # the attribution itself is published after release.
                    released_request_id = state.request_id
                    released_operation = state.operation
                    released_holder_kind = state.holder_kind
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
                # Contention attribution AFTER release, for the same reason the
                # telemetry below is: measurement must never extend the
                # critical section other writers are polling on.
                _note_release(
                    state,
                    request_id=released_request_id,
                    operation=released_operation,
                    holder_kind=released_holder_kind,
                )
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
        """Measure this vault's OS boundary without exposing identity or content.

        The returned block always carries an additive, content-free
        `contention` sub-block.  The `state` flag is one instantaneous probe;
        the counters are the only part of this payload that can show a bounded
        waiter losing to a stream of short holds.
        """
        state = _state_for(self.lock_path)
        probed = self._probe(state)
        probed["contention"] = _contention_view(state)
        return probed

    def _refused(self, wait_start: float) -> OpError:
        """Record one refusal, then build MUTATION_BUSY from that same probe."""
        state = _state_for(self.lock_path)
        probed = self._probe(state)
        _note_busy_refusal(state, probed)
        probed["contention"] = _contention_view(state)
        return _mutation_busy(probed, wait_ms=(time.monotonic() - wait_start) * 1000)

    def _probe(self, state: _LocalLockState) -> dict[str, object]:
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
            prepare_windows_private_state_root(self.state_root)
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
        publish_holder_metadata: bool,
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
                raise self._refused(wait_start)
            acquired = False
            try:
                try:
                    acquired = _try_os_lock(handle)
                except OSError as exc:
                    raise self._lock_unavailable(exc) from None
                if acquired:
                    try:
                        if publish_holder_metadata:
                            holder = {
                                "schema": _HOLDER_SCHEMA,
                                "generation": uuid.uuid4().hex,
                                "request_id": _safe_label(request_id, fallback="untracked"),
                                "operation": _safe_label(operation, fallback="unknown"),
                                "holder_kind": _safe_label(holder_kind, fallback="unknown"),
                                "acquired_at": time.time(),
                                "long_holder_seconds": self.long_holder_seconds,
                            }
                            self._publish_holder_metadata(holder)
                        else:
                            _clear_holder_metadata(self.metadata_path)
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
                raise self._refused(wait_start)
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
        getattr(msvcrt, "locking", None) if os.name == "nt" else None  # noqa: B008 - injectable platform seam
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
        getattr(msvcrt, "locking", None) if os.name == "nt" else None  # noqa: B008 - injectable platform seam
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


def process_local_mutation_boundary() -> dict[str, object]:
    """Report the process-local view honestly: a local hold, or `unknown`.

    `active_mutation_snapshot()` walks this process's own lock states only, so
    its "free" means "nobody *here* holds a boundary" — it cannot see another
    process's hold, and reporting that absence as a verified `free` is exactly
    how readiness came to contradict live MUTATION_BUSY refusals.  A local hold
    is real and stays `held`; anything else is `unknown`.
    """
    local = active_mutation_snapshot()
    if local.get("state") == "held":
        return local
    return {"state": "unknown", "reason": "process_local_only"}


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
    contention = snapshot.get("contention") if snapshot else None
    if snapshot and snapshot.get("state") == "held":
        # The holder block keeps exactly the keys it always had; the
        # attribution fields are added alongside it, not inside it.
        details["holder"] = {
            key: value for key, value in snapshot.items() if key != "contention"
        }
    if isinstance(contention, Mapping):
        details["acquire_attempts"] = contention.get("acquire_attempts")
        details["busy_refusals"] = contention.get("busy_refusals")
        details["busy_refusals_recent"] = contention.get("busy_refusals_recent")
        details["contention_scope"] = contention.get("scope")
        last_holder = contention.get("last_holder")
        if last_holder is not None:
            details["last_holder"] = last_holder
    _bump_boundary_metric("exomem_mutation_busy_total", {"code": "MUTATION_BUSY"})
    return OpError(
        "MUTATION_BUSY",
        "vault mutation boundary is busy",
        "Retry after the current mutation completes; inspect cell health if it remains busy.",
        details=details,
    )
