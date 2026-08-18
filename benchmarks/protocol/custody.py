"""Narrow POSIX directory capability custody for direct-provider lifecycles.

The caller-supplied run directory remains the trust anchor.  This module holds
individual directory edges below that anchor so lifecycle authority never
falls back to a pathname after provider construction.
"""

from __future__ import annotations

import hashlib
import os
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path


class CustodyError(RuntimeError):
    """A held capability could not safely complete an operation."""


class CustodyUnsupported(CustodyError):
    """The current platform/filesystem cannot provide the required custody."""


class CustodyBindingLost(CustodyError):
    """A held inode is no longer bound at its original parent/name edge."""


class CustodyLimitExceeded(CustodyError):
    """A bounded custody operation exceeded its declared limit."""


@dataclass(frozen=True)
class PublishedFile:
    payload: bytes
    sha256: str
    device: int
    inode: int


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    # Guarded like every other flag here. Unguarded it raised `AttributeError`
    # at *import* on Windows, so callers got a missing-attribute crash instead
    # of this module's own `CustodyUnsupported` -- an absent capability
    # reported as a bug in `os`. The refusal is in `prove_supported`, which is
    # where it belongs and where it can say what is missing.
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_READ_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


def _component(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise CustodyError("custody operation requires one safe path component")
    return value


def _logical_ref(value: Path | str) -> Path:
    path = Path(value)
    text = path.as_posix()
    if path.is_absolute() or "\\" in str(value) or any(part == ".." for part in path.parts):
        raise CustodyError("custody logical reference must be canonical and relative")
    if text not in {"."} and (not text or text.endswith("/") or "//" in text):
        raise CustodyError("custody logical reference must be canonical and relative")
    return path


def _open_directory(name: str, *, dir_fd: int | None = None) -> int:
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise CustodyError("directory capability cannot be opened") from exc


def _identity(descriptor: int) -> tuple[int, int]:
    status = os.fstat(descriptor)
    if not stat.S_ISDIR(status.st_mode):
        raise CustodyError("directory capability is not a directory")
    return status.st_dev, status.st_ino


@dataclass
class HeldDirectory:
    """A retained parent/name/child edge and its immutable inode identity."""

    parent_fd: int
    name: str
    fd: int
    device: int
    inode: int
    logical_ref: Path
    _parent_holder: HeldDirectory | None = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def capability_path(self) -> Path:
        if self._closed:
            raise CustodyError("directory capability is closed")
        return Path(f"/proc/{os.getpid()}/fd/{self.fd}")

    def __enter__(self) -> HeldDirectory:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for descriptor in (self.fd, self.parent_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _ensure_open(self) -> None:
        if self._closed:
            raise CustodyError("directory capability is closed")

    def assert_bound(self) -> None:
        self._ensure_open()
        if self._parent_holder is not None:
            self._parent_holder.assert_bound()
        try:
            held = os.fstat(self.fd)
            named = os.stat(self.name, dir_fd=self.parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise CustodyBindingLost("directory capability binding is unavailable") from exc
        if (
            not stat.S_ISDIR(held.st_mode)
            or not stat.S_ISDIR(named.st_mode)
            or (held.st_dev, held.st_ino) != (self.device, self.inode)
            or (named.st_dev, named.st_ino) != (self.device, self.inode)
        ):
            raise CustodyBindingLost("directory capability binding changed")

    def mkdir(
        self,
        component: str,
        *,
        logical_ref: Path | str,
        mode: int = 0o700,
    ) -> HeldDirectory:
        name = _component(component)
        logical = _logical_ref(logical_ref)
        self.assert_bound()
        try:
            os.mkdir(name, mode=mode, dir_fd=self.fd)
        except OSError as exc:
            raise CustodyError("held child directory cannot be created") from exc
        try:
            child_fd = _open_directory(name, dir_fd=self.fd)
            device, inode = _identity(child_fd)
            parent_fd = os.dup(self.fd)
        except BaseException:
            try:
                os.close(child_fd)
            except (OSError, UnboundLocalError):
                pass
            try:
                os.rmdir(name, dir_fd=self.fd)
            except OSError:
                pass
            raise
        child = HeldDirectory(
            parent_fd=parent_fd,
            name=name,
            fd=child_fd,
            device=device,
            inode=inode,
            logical_ref=logical,
            _parent_holder=self,
        )
        try:
            child.assert_bound()
        except BaseException:
            child.close()
            raise
        return child

    def open_dir(self, component: str, *, logical_ref: Path | str) -> HeldDirectory:
        name = _component(component)
        logical = _logical_ref(logical_ref)
        self.assert_bound()
        child_fd: int | None = None
        try:
            child_fd = _open_directory(name, dir_fd=self.fd)
            device, inode = _identity(child_fd)
            parent_fd = os.dup(self.fd)
        except BaseException:
            if child_fd is not None:
                os.close(child_fd)
            raise
        child = HeldDirectory(
            parent_fd=parent_fd, name=name, fd=child_fd,
            device=device, inode=inode, logical_ref=logical, _parent_holder=self,
        )
        try:
            child.assert_bound()
        except BaseException:
            child.close()
            raise
        return child

    def read_regular_bounded(
        self,
        component: str,
        *,
        max_bytes: int,
        with_identity: bool = False,
    ) -> bytes | tuple[bytes, tuple[int, int]]:
        name = _component(component)
        if max_bytes < 0:
            raise CustodyLimitExceeded("bounded read limit is invalid")
        self.assert_bound()
        try:
            descriptor = os.open(name, _READ_FLAGS, dir_fd=self.fd)
        except OSError as exc:
            raise CustodyError("held file cannot be opened safely") from exc
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise CustodyError("held file is not regular")
            if opened.st_size > max_bytes:
                raise CustodyLimitExceeded("held file exceeds bounded read limit")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > max_bytes:
                raise CustodyLimitExceeded("held file exceeds bounded read limit")
            named = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(named.st_mode)
                or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                raise CustodyBindingLost("held file binding changed during read")
        finally:
            os.close(descriptor)
        self.assert_bound()
        if with_identity:
            return payload, (opened.st_dev, opened.st_ino)
        return payload

    def publish_exclusive(
        self,
        component: str,
        payload: bytes,
        *,
        max_bytes: int,
    ) -> PublishedFile:
        name = _component(component)
        if len(payload) > max_bytes:
            raise CustodyLimitExceeded("publication exceeds bounded size")
        self.assert_bound()
        temporary = f".custody-{uuid.uuid4().hex}.tmp"
        descriptor: int | None = None
        temporary_created = False
        published = False
        try:
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=self.fd,
                )
                temporary_created = True
            except OSError as exc:
                raise CustodyError("exclusive publication temporary cannot be created") from exc
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise CustodyError("exclusive publication write did not progress")
                view = view[written:]
            os.fsync(descriptor)
            staged = os.fstat(descriptor)
            if not stat.S_ISREG(staged.st_mode):
                raise CustodyError("exclusive publication temporary is not regular")
            self.assert_bound()
            try:
                os.link(
                    temporary, name,
                    src_dir_fd=self.fd, dst_dir_fd=self.fd,
                    follow_symlinks=False,
                )
                published = True
            except OSError as exc:
                raise CustodyError("exclusive publication target already exists or is unavailable") from exc
            os.fsync(self.fd)
            reopened = self.read_regular_bounded(name, max_bytes=max_bytes)
            named = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if reopened != payload or (named.st_dev, named.st_ino) != (staged.st_dev, staged.st_ino):
                raise CustodyError("exclusive publication did not reopen exact bytes and inode")
            self.assert_bound()
            return PublishedFile(
                payload=reopened,
                sha256=hashlib.sha256(reopened).hexdigest(),
                device=named.st_dev,
                inode=named.st_ino,
            )
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temporary_created:
                try:
                    os.unlink(temporary, dir_fd=self.fd)
                    os.fsync(self.fd)
                except OSError:
                    pass
            # A linked final is evidence even if a later validation fails.  It
            # is intentionally never removed here.
            del published

    def prove_supported(self) -> None:
        self.assert_bound()
        if os.name != "posix" or not Path("/proc/self/fd").is_dir():
            raise CustodyUnsupported("POSIX proc-fd directory capabilities are unavailable")
        probe_name = f".custody-proof-{uuid.uuid4().hex}"
        probe: HeldDirectory | None = None
        probe_identity: tuple[int, int] | None = None
        try:
            probe = self.mkdir(probe_name, logical_ref=self.logical_ref / probe_name)
            probe_identity = (probe.device, probe.inode)
            try:
                reopened = os.open(
                    probe.capability_path,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0),
                )
            except OSError as exc:
                raise CustodyUnsupported("proc-fd directory capability cannot be reopened") from exc
            try:
                if _identity(reopened) != (probe.device, probe.inode):
                    raise CustodyUnsupported("proc-fd directory reopen changed identity")
            finally:
                os.close(reopened)
            result = probe.publish_exclusive("probe", b"custody-proof", max_bytes=64)
            if result.payload != b"custody-proof":
                raise CustodyUnsupported("custody publication proof disagreed")
            probe.empty_recursive(max_entries=8, max_depth=2)
            if not probe.retire(max_entries=8, max_depth=2):
                raise CustodyUnsupported("custody retirement proof lost binding")
        except CustodyUnsupported:
            raise
        except BaseException as exc:
            raise CustodyUnsupported("required directory custody operations are unavailable") from exc
        finally:
            if probe is not None:
                probe.close()
            if probe_identity is not None:
                quarantine_fd: int | None = None
                try:
                    quarantine_fd, quarantine_name, quarantine_identity = _open_private_quarantine(
                        self._private_quarantine_parent_fd(),
                    )
                    _quarantine_and_remove(
                        self.fd,
                        probe_name,
                        expected=probe_identity,
                        directory=True,
                        quarantine_fd=quarantine_fd,
                    )
                except CustodyError:
                    pass
                finally:
                    if quarantine_fd is not None:
                        _close_private_quarantine(
                            self._private_quarantine_parent_fd(),
                            quarantine_fd,
                            quarantine_name,
                            quarantine_identity,
                        )
        self.assert_bound()

    def empty_recursive(self, *, max_entries: int, max_depth: int) -> None:
        self._ensure_open()
        if max_entries < 0 or max_depth < 0:
            raise CustodyLimitExceeded("recursive custody limits are invalid")
        quarantine_fd, quarantine_name, quarantine_identity = _open_private_quarantine(
            self._private_quarantine_parent_fd(),
        )
        remaining = [max_entries]
        try:
            _empty_directory_fd(
                self.fd,
                remaining=remaining,
                depth=max_depth,
                quarantine_fd=quarantine_fd,
            )
        finally:
            _close_private_quarantine(
                self._private_quarantine_parent_fd(),
                quarantine_fd,
                quarantine_name,
                quarantine_identity,
            )

    def retire(self, *, max_entries: int, max_depth: int) -> bool:
        self.empty_recursive(max_entries=max_entries, max_depth=max_depth)
        try:
            self.assert_bound()
        except CustodyBindingLost:
            return False
        try:
            quarantine_fd, quarantine_name, quarantine_identity = _open_private_quarantine(
                self._private_quarantine_parent_fd(),
            )
            _quarantine_and_remove(
                self.parent_fd,
                self.name,
                expected=(self.device, self.inode),
                directory=True,
                quarantine_fd=quarantine_fd,
            )
        except CustodyBindingLost:
            return False
        finally:
            if "quarantine_fd" in locals():
                _close_private_quarantine(
                    self._private_quarantine_parent_fd(),
                    quarantine_fd,
                    quarantine_name,
                    quarantine_identity,
                )
        return True

    def _private_quarantine_parent_fd(self) -> int:
        if self._parent_holder is not None:
            return self._parent_holder.parent_fd
        return self.parent_fd


def _empty_directory_fd(
    descriptor: int,
    *,
    remaining: list[int],
    depth: int,
    quarantine_fd: int,
) -> None:
    try:
        with os.scandir(descriptor) as iterator:
            names = sorted(entry.name for entry in iterator)
    except OSError as exc:
        raise CustodyError("held directory cannot be inventoried") from exc
    for name in names:
        remaining[0] -= 1
        if remaining[0] < 0:
            raise CustodyLimitExceeded("recursive custody entry limit exceeded")
        try:
            before = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as exc:
            raise CustodyBindingLost("held child disappeared during retirement") from exc
        if stat.S_ISDIR(before.st_mode):
            if depth <= 0:
                raise CustodyLimitExceeded("recursive custody depth limit exceeded")
            child_fd = _open_directory(name, dir_fd=descriptor)
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                    raise CustodyBindingLost("held child binding changed during retirement")
                _empty_directory_fd(
                    child_fd,
                    remaining=remaining,
                    depth=depth - 1,
                    quarantine_fd=quarantine_fd,
                )
                _quarantine_and_remove(
                    descriptor,
                    name,
                    expected=(opened.st_dev, opened.st_ino),
                    directory=True,
                    quarantine_fd=quarantine_fd,
                )
            finally:
                os.close(child_fd)
        else:
            try:
                _quarantine_and_remove(
                    descriptor,
                    name,
                    expected=(before.st_dev, before.st_ino),
                    directory=False,
                    quarantine_fd=quarantine_fd,
                )
            except OSError as exc:
                raise CustodyError("held non-directory entry cannot be unlinked") from exc


def _quarantine_and_remove(
    parent_fd: int,
    name: str,
    *,
    expected: tuple[int, int],
    directory: bool,
    quarantine_fd: int,
) -> None:
    """Atomically move a named entry aside, bind it, then remove only that inode."""

    quarantine = uuid.uuid4().hex
    moved = False
    try:
        os.rename(name, quarantine, src_dir_fd=parent_fd, dst_dir_fd=quarantine_fd)
        moved = True
    except FileNotFoundError as exc:
        raise CustodyBindingLost("held child disappeared during retirement") from exc
    except OSError as exc:
        raise CustodyError("held child cannot be quarantined for retirement") from exc
    try:
        quarantined = os.stat(quarantine, dir_fd=quarantine_fd, follow_symlinks=False)
        if (quarantined.st_dev, quarantined.st_ino) != expected:
            try:
                os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                try:
                    os.rename(quarantine, name, src_dir_fd=quarantine_fd, dst_dir_fd=parent_fd)
                    moved = False
                except OSError:
                    pass
            raise CustodyBindingLost("held child binding changed during retirement")
        if directory:
            os.rmdir(quarantine, dir_fd=quarantine_fd)
        else:
            os.unlink(quarantine, dir_fd=quarantine_fd)
        moved = False
    except CustodyBindingLost:
        raise
    except OSError as exc:
        raise CustodyError("held child cannot be removed after quarantine") from exc
    finally:
        # A verified held inode may remain quarantined after a failed delete;
        # it is safer to leave it than to guess at a later pathname binding.
        del moved


def _open_private_quarantine(parent_fd: int) -> tuple[int, str, tuple[int, int]]:
    for _ in range(8):
        name = f".custody-private-{uuid.uuid4().hex}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        except OSError as exc:
            raise CustodyError("private retirement quarantine cannot be created") from exc
        try:
            descriptor = _open_directory(name, dir_fd=parent_fd)
            identity = _identity(descriptor)
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != identity:
                raise CustodyBindingLost("private retirement quarantine binding changed")
            return descriptor, name, identity
        except BaseException:
            try:
                os.close(descriptor)
            except (OSError, UnboundLocalError):
                pass
            try:
                os.rmdir(name, dir_fd=parent_fd)
            except OSError:
                pass
            raise
    raise CustodyError("private retirement quarantine cannot be created")


def _close_private_quarantine(
    parent_fd: int,
    descriptor: int,
    name: str,
    identity: tuple[int, int],
) -> None:
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (named.st_dev, named.st_ino) == identity:
            os.rmdir(name, dir_fd=parent_fd)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def retire_child_directory(
    root: Path | str,
    component: str,
    *,
    max_entries: int,
    max_depth: int,
) -> bool:
    """Retire one provider-owned child through an already-held root capability path."""

    name = _component(component)
    if max_entries < 0 or max_depth < 0:
        raise CustodyLimitExceeded("recursive custody limits are invalid")
    root_path = Path(root)
    capability_prefix = f"/proc/{os.getpid()}/fd/"
    flags = _DIRECTORY_FLAGS
    if str(root_path).startswith(capability_prefix):
        # proc-fd entries are symlinks by design; reopening is separately
        # identity-checked by the runner-owned holder around this operation.
        flags &= ~getattr(os, "O_NOFOLLOW", 0)
    try:
        root_fd = os.open(root_path, flags)
    except OSError as exc:
        raise CustodyError("provider cleanup root cannot be opened safely") from exc
    child_fd: int | None = None
    parent_fd: int | None = None
    quarantine_fd: int | None = None
    quarantine_name: str | None = None
    quarantine_identity: tuple[int, int] | None = None
    try:
        try:
            before = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(before.st_mode):
            raise CustodyError("provider cleanup child is not a directory")
        child_fd = _open_directory(name, dir_fd=root_fd)
        opened = os.fstat(child_fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise CustodyBindingLost("provider cleanup child binding changed")
        parent_fd = _open_directory("..", dir_fd=root_fd)
        quarantine_fd, quarantine_name, quarantine_identity = _open_private_quarantine(parent_fd)
        _empty_directory_fd(
            child_fd,
            remaining=[max_entries],
            depth=max_depth,
            quarantine_fd=quarantine_fd,
        )
        _quarantine_and_remove(
            root_fd,
            name,
            expected=(opened.st_dev, opened.st_ino),
            directory=True,
            quarantine_fd=quarantine_fd,
        )
        return True
    except OSError as exc:
        raise CustodyError("provider cleanup child could not be retired") from exc
    finally:
        if (
            parent_fd is not None
            and quarantine_fd is not None
            and quarantine_name is not None
            and quarantine_identity is not None
        ):
            _close_private_quarantine(
                parent_fd, quarantine_fd, quarantine_name, quarantine_identity,
            )
        if parent_fd is not None:
            os.close(parent_fd)
        if child_fd is not None:
            os.close(child_fd)
        os.close(root_fd)


def hold_directory(
    path: Path | str,
    *,
    create: bool = False,
    mode: int = 0o700,
    logical_ref: Path | str,
) -> HeldDirectory:
    """Open one directory path no-follow and retain its final edge."""

    logical = _logical_ref(logical_ref)
    absolute = Path(path).absolute()
    parts = absolute.parts
    if not parts or parts[0] != os.sep or len(parts) < 2:
        raise CustodyError("custody path must identify a non-root directory")
    if any(part in {"", ".", ".."} or "\\" in part for part in parts[1:]):
        raise CustodyError("custody path contains an unsafe component")
    current = _open_directory(os.sep)
    try:
        for part in parts[1:-1]:
            next_fd = _open_directory(part, dir_fd=current)
            os.close(current)
            current = next_fd
        name = _component(parts[-1])
        if create:
            try:
                os.mkdir(name, mode=mode, dir_fd=current)
            except OSError as exc:
                raise CustodyError("custody root cannot be created exclusively") from exc
        child_fd = _open_directory(name, dir_fd=current)
        device, inode = _identity(child_fd)
        held = HeldDirectory(
            parent_fd=current,
            name=name,
            fd=child_fd,
            device=device,
            inode=inode,
            logical_ref=logical,
        )
        current = -1
        held.assert_bound()
        return held
    finally:
        if current >= 0:
            os.close(current)
