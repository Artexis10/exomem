"""Bounded tenant-local temporary state for hosted compatibility consumers."""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .hosted_transfer import TRANSFER_RUNTIME_TEMP_QUOTA_BYTES

_MAX_RUNTIME_TEMP_ENTRIES = 10_000
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class HostedRuntimeTempUnavailable(RuntimeError):
    code = "HOSTED_RUNTIME_TEMP_UNAVAILABLE"


def _private_directory(path: Path, *, expected_uid: int, expected_gid: int) -> None:
    try:
        path.mkdir(mode=0o700, parents=False, exist_ok=True)
        value = path.lstat()
        if (
            stat.S_ISLNK(value.st_mode)
            or not stat.S_ISDIR(value.st_mode)
            or value.st_uid != expected_uid
            or value.st_gid != expected_gid
        ):
            raise OSError("runtime temp directory has unsafe ownership")
        path.chmod(0o700, follow_symlinks=False)
        if stat.S_IMODE(path.lstat().st_mode) != 0o700:
            raise OSError("runtime temp directory is not private")
    except OSError as exc:
        raise HostedRuntimeTempUnavailable from exc


def _open_root(root: Path, *, expected_uid: int, expected_gid: int) -> int:
    try:
        descriptor = os.open(root, _DIRECTORY_FLAGS)
        value = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(value.st_mode)
            or value.st_uid != expected_uid
            or value.st_gid != expected_gid
            or stat.S_IMODE(value.st_mode) != 0o700
        ):
            raise OSError("runtime temp root is unsafe")
        return descriptor
    except OSError as exc:
        try:
            os.close(descriptor)
        except (OSError, UnboundLocalError):
            pass
        raise HostedRuntimeTempUnavailable from exc


def _clear_directory(descriptor: int, *, visited: list[int]) -> None:
    try:
        names = sorted(os.listdir(descriptor))
    except OSError as exc:
        raise HostedRuntimeTempUnavailable from exc
    for name in names:
        visited[0] += 1
        if visited[0] > _MAX_RUNTIME_TEMP_ENTRIES:
            raise HostedRuntimeTempUnavailable
        try:
            value = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(value.st_mode):
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                try:
                    _clear_directory(child, visited=visited)
                finally:
                    os.close(child)
                os.rmdir(name, dir_fd=descriptor)
            else:
                os.unlink(name, dir_fd=descriptor)
        except HostedRuntimeTempUnavailable:
            raise
        except OSError as exc:
            raise HostedRuntimeTempUnavailable from exc
    try:
        os.fsync(descriptor)
    except OSError as exc:
        raise HostedRuntimeTempUnavailable from exc


def prepare_hosted_runtime_temp(
    state_root: Path | str,
    *,
    expected_uid: int,
    expected_gid: int,
) -> Path:
    """Create and no-follow clear the disposable runtime root during locked startup."""

    root = ensure_hosted_runtime_temp(
        state_root,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    descriptor = _open_root(root, expected_uid=expected_uid, expected_gid=expected_gid)
    try:
        _clear_directory(descriptor, visited=[0])
    finally:
        os.close(descriptor)
    return root


def ensure_hosted_runtime_temp(
    state_root: Path | str,
    *,
    expected_uid: int,
    expected_gid: int,
) -> Path:
    """Validate or create the private runtime root without deleting any entry."""

    state = Path(state_root)
    if not state.is_absolute():
        raise HostedRuntimeTempUnavailable
    try:
        state_value = state.lstat()
    except OSError as exc:
        raise HostedRuntimeTempUnavailable from exc
    if stat.S_ISLNK(state_value.st_mode) or not stat.S_ISDIR(state_value.st_mode):
        raise HostedRuntimeTempUnavailable
    temporary = state / "tmp"
    _private_directory(temporary, expected_uid=expected_uid, expected_gid=expected_gid)
    root = temporary / "runtime"
    _private_directory(root, expected_uid=expected_uid, expected_gid=expected_gid)
    return root


def _measure_directory(
    descriptor: int,
    *,
    expected_uid: int,
    expected_gid: int,
    visited: list[int],
) -> int:
    total = 0
    try:
        names = sorted(os.listdir(descriptor))
    except OSError as exc:
        raise HostedRuntimeTempUnavailable from exc
    for name in names:
        visited[0] += 1
        if visited[0] > _MAX_RUNTIME_TEMP_ENTRIES:
            raise HostedRuntimeTempUnavailable
        try:
            value = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if value.st_uid != expected_uid or value.st_gid != expected_gid:
                raise HostedRuntimeTempUnavailable
            if stat.S_IMODE(value.st_mode) & 0o077:
                raise HostedRuntimeTempUnavailable
            if stat.S_ISDIR(value.st_mode):
                child = os.open(name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                try:
                    total += _measure_directory(
                        child,
                        expected_uid=expected_uid,
                        expected_gid=expected_gid,
                        visited=visited,
                    )
                finally:
                    os.close(child)
            elif stat.S_ISREG(value.st_mode) and value.st_nlink == 1:
                total += value.st_size
            else:
                raise HostedRuntimeTempUnavailable
        except HostedRuntimeTempUnavailable:
            raise
        except OSError as exc:
            raise HostedRuntimeTempUnavailable from exc
        if total > TRANSFER_RUNTIME_TEMP_QUOTA_BYTES:
            raise HostedRuntimeTempUnavailable
    return total


class HostedRuntimeTempAuthority:
    """Process-local reservations backed by no-follow durable usage checks."""

    def __init__(self, root: Path | str, *, expected_uid: int, expected_gid: int) -> None:
        self.root = Path(root)
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self._lock = threading.Lock()
        self._reserved_bytes = 0
        descriptor = _open_root(
            self.root,
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
        )
        os.close(descriptor)

    def _used_bytes(self) -> int:
        descriptor = _open_root(
            self.root,
            expected_uid=self.expected_uid,
            expected_gid=self.expected_gid,
        )
        try:
            return _measure_directory(
                descriptor,
                expected_uid=self.expected_uid,
                expected_gid=self.expected_gid,
                visited=[0],
            )
        finally:
            os.close(descriptor)

    @contextmanager
    def reserve(self, maximum_bytes: int) -> Iterator[Path]:
        if (
            isinstance(maximum_bytes, bool)
            or not isinstance(maximum_bytes, int)
            or maximum_bytes <= 0
            or maximum_bytes > TRANSFER_RUNTIME_TEMP_QUOTA_BYTES
        ):
            raise HostedRuntimeTempUnavailable
        with self._lock:
            used = self._used_bytes()
            if used + self._reserved_bytes + maximum_bytes > TRANSFER_RUNTIME_TEMP_QUOTA_BYTES:
                raise HostedRuntimeTempUnavailable
            self._reserved_bytes += maximum_bytes
        try:
            yield self.root
        finally:
            with self._lock:
                self._reserved_bytes -= maximum_bytes


__all__ = [
    "HostedRuntimeTempAuthority",
    "HostedRuntimeTempUnavailable",
    "ensure_hosted_runtime_temp",
    "prepare_hosted_runtime_temp",
]
