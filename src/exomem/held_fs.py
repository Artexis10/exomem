"""Closed, handle-relative filesystem primitives for cooperating subsystems.

This module deliberately exposes no path-based descendant operation.  A caller
first acquires a root anchor, then works through retained parent handles.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, TypeVar

T = TypeVar("T")
_MISSING = object()
PUBLISH_TEMP_PREFIX = ".exomem-held-publish-"
_CAPABILITY_CACHE: dict[tuple[int, str, int, int], Capabilities] = {}
_CAPABILITY_CACHE_LOCK = threading.Lock()


@dataclass(slots=True)
class HeldFsError(Exception):
    """A typed, content-free refusal from a held filesystem operation."""

    code: str
    detail: str
    cause: OSError | None = field(default=None, repr=False, compare=False)
    source_leaf: str | None = field(default=None, repr=False, compare=False)
    destination_leaf: str | None = field(default=None, repr=False, compare=False)

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


@dataclass(frozen=True, slots=True)
class HeldResult(Generic[T]):
    """The closed result shape used by every public primitive operation."""

    value: T | object = _MISSING
    error: HeldFsError | None = None

    def __post_init__(self) -> None:
        if (self.value is _MISSING) == (self.error is None):
            raise ValueError("held result must contain exactly one outcome")

    @property
    def ok(self) -> bool:
        return self.error is None

    def require(self) -> T:
        if self.error is not None:
            raise self.error
        return self.value  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Capabilities proven against the requested filesystem, not the host alone."""

    relative_operations: bool
    no_follow: bool
    stable_identity: bool
    same_device_rename: bool
    hard_link: bool
    reason: str = ""

    @classmethod
    def disabled(cls, reason: str) -> Capabilities:
        return cls(False, False, False, False, False, reason)


@dataclass(frozen=True, slots=True)
class StableIdentity:
    """Stable identity derived from the handle already used for the operation."""

    device: int
    inode: int
    kind: str
    link_count: int


def _same_file_identity(first: StableIdentity, second: StableIdentity) -> bool:
    return (
        first.device == second.device
        and first.inode == second.inode
        and first.kind == second.kind
    )


@dataclass(frozen=True, slots=True)
class SagaRecord:
    """One deterministic recursive enumeration effect/observation record."""

    relative_path: str
    identity: StableIdentity


class HeldDirectory:
    """A retained directory handle.  Instances are created by ``parent`` only."""

    identity: StableIdentity

    def __enter__(self) -> HeldDirectory:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        raise NotImplementedError


class HeldFile:
    """A retained regular-file handle and the identity observed from that handle."""

    identity: StableIdentity

    def __enter__(self) -> HeldFile:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        raise NotImplementedError


class HeldFilesystem:
    """A retained vault-root anchor with handle-relative leaf operations."""

    def __enter__(self) -> HeldFilesystem:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        raise NotImplementedError

    def parent(
        self,
        relative: str,
        *,
        create: bool = False,
        exclusive: bool = False,
        access: str = "read",
    ) -> HeldResult[HeldDirectory]:
        raise NotImplementedError

    def file(
        self,
        parent: HeldDirectory,
        leaf: str,
        *,
        access: str = "read",
        create: bool = False,
        exclusive: bool = False,
    ) -> HeldResult[HeldFile]:
        raise NotImplementedError

    def validate_directory(
        self,
        directory: HeldDirectory,
        *,
        require_name: bool = True,
    ) -> HeldResult[None]:
        raise NotImplementedError

    def read(self, file: HeldFile) -> HeldResult[bytes]:
        raise NotImplementedError

    def write(self, file: HeldFile, data: bytes) -> HeldResult[None]:
        raise NotImplementedError

    def flush_directory(self, directory: HeldDirectory) -> HeldResult[None]:
        raise NotImplementedError

    def rename(
        self,
        source: HeldFile,
        destination_parent: HeldDirectory,
        destination_leaf: str,
        *,
        replace: bool = False,
    ) -> HeldResult[None]:
        raise NotImplementedError

    def rename_directory(
        self,
        source: HeldDirectory,
        destination_parent: HeldDirectory,
        destination_leaf: str,
    ) -> HeldResult[None]:
        raise NotImplementedError

    def link(
        self,
        source: HeldFile,
        destination_parent: HeldDirectory,
        destination_leaf: str,
    ) -> HeldResult[None]:
        raise NotImplementedError

    def unlink(self, file: HeldFile) -> HeldResult[None]:
        raise NotImplementedError

    def unlink_directory(self, directory: HeldDirectory) -> HeldResult[None]:
        raise NotImplementedError

    def copy(
        self,
        source: HeldFile,
        destination_parent: HeldDirectory,
        destination_leaf: str,
    ) -> HeldResult[None]:
        raise NotImplementedError

    def children(self, parent: HeldDirectory) -> HeldResult[tuple[SagaRecord, ...]]:
        """Return stable immediate children, omitting unsafe alias objects."""

        raise NotImplementedError

    def enumerate(self, parent: HeldDirectory) -> HeldResult[tuple[SagaRecord, ...]]:
        raise NotImplementedError


def _backend():
    if os.name == "nt":  # pragma: no cover - exercised by the Windows CI gate
        from . import _held_fs_windows as backend
    else:
        from . import _held_fs_posix as backend
    return backend


def _capability_cache_key(root: Path) -> tuple[int, str, int, int] | None:
    try:
        info = root.lstat()
    except OSError:
        return None
    return (
        os.getpid(),
        os.path.normcase(os.fspath(root.absolute())),
        int(info.st_dev),
        int(info.st_ino),
    )


def probe(root: Path) -> Capabilities:
    """Probe one stable root once per process; no failed probe gets a fallback."""
    root = Path(root)
    key = _capability_cache_key(root)
    if key is None:
        return _backend().probe(root)
    with _CAPABILITY_CACHE_LOCK:
        cached = _CAPABILITY_CACHE.get(key)
        if cached is not None:
            return cached
        capability = _backend().probe(root)
        _CAPABILITY_CACHE[key] = capability
        return capability


def acquire(root: Path) -> HeldResult[HeldFilesystem]:
    """Acquire a root anchor or return a content-free capability/IO refusal."""
    root = Path(root)
    return _backend().acquire(root, capability=probe(root))


def flush_directory_path(path: Path) -> HeldResult[None]:
    """Durably flush one exact no-follow directory path.

    The ordinary held API flushes descendant handles.  External owner files
    can live directly under the acquired anchor, where Windows deliberately
    refuses an elevated root handle.  Reuse the platform's secure directory
    open for that one root-level durability cut instead of silently skipping
    it or importing migration code into owner operations.
    """

    from . import mutation_lock

    try:
        directory = Path(path)
        if os.name == "nt":
            mutation_lock._windows_flush_directory(directory)
        else:
            with mutation_lock._open_secure_directory(
                directory, create=False
            ) as retained:
                if retained.fd is None:
                    raise OSError("directory durability handle is unavailable")
                os.fsync(retained.fd)
        return HeldResult(value=None)
    except OSError as error:
        return HeldResult(
            error=HeldFsError(
                "IO_REFUSED",
                "directory durability flush was refused",
                cause=error,
            )
        )


def reset_capability_cache_for_tests() -> None:
    """Clear process probe state for deterministic backend contract tests."""
    with _CAPABILITY_CACHE_LOCK:
        _CAPABILITY_CACHE.clear()


def publish_bytes(
    filesystem: HeldFilesystem,
    parent: HeldDirectory,
    leaf: str,
    data: bytes,
    *,
    expected_identity: StableIdentity | None = None,
    expected_sha256: str | None = None,
    prepare: Callable[[HeldFile], None] | None = None,
) -> HeldResult[StableIdentity]:
    """Atomically publish bytes under one held parent.

    With no expected identity the publish is create-only and uses a no-replace
    hard-link installation. With an expected identity, that exact destination
    stays held while a native relative replacement installs the staged bytes.
    """

    if not isinstance(data, bytes):
        return HeldResult(error=HeldFsError("INVALID_INPUT", "data must be bytes"))
    existing: HeldFile | None = None
    staged: HeldFile | None = None
    temporary = f"{PUBLISH_TEMP_PREFIX}{secrets.token_hex(16)}"
    temporary_identity: StableIdentity | None = None

    def publication_error(error: HeldFsError | None) -> HeldResult[StableIdentity]:
        bounded = error or HeldFsError("IO_REFUSED", "publish was refused")
        bounded.source_leaf = temporary
        bounded.destination_leaf = leaf
        return HeldResult(error=bounded)

    try:
        current = filesystem.file(parent, leaf)
        if expected_identity is None:
            if current.ok:
                current.require().close()
                return HeldResult(
                    error=HeldFsError("DESTINATION_EXISTS", "destination already exists")
                )
            if current.error is None or current.error.code != "MISSING":
                return HeldResult(error=current.error or HeldFsError("IO_REFUSED", "publish refused"))
        else:
            if not current.ok:
                return HeldResult(error=current.error)
            existing = current.require()
            if existing.identity != expected_identity or existing.identity.link_count != 1:
                return HeldResult(
                    error=HeldFsError("IDENTITY_CHANGED", "held filesystem identity changed")
                )
            if expected_sha256 is not None:
                observed = filesystem.read(existing)
                if (
                    not observed.ok
                    or hashlib.sha256(observed.require()).hexdigest()
                    != expected_sha256
                ):
                    return HeldResult(
                        error=HeldFsError(
                            "IDENTITY_CHANGED", "held filesystem content changed"
                        )
                    )

        created = filesystem.file(
            parent,
            temporary,
            access="write",
            create=True,
            exclusive=True,
        )
        if not created.ok:
            return HeldResult(error=created.error)
        with created.require() as writable:
            written = filesystem.write(writable, data)
            if not written.ok:
                return HeldResult(error=written.error)
            if prepare is not None:
                prepare(writable)
            temporary_identity = writable.identity

        mutable = filesystem.file(parent, temporary, access="mutate")
        if not mutable.ok:
            return HeldResult(error=mutable.error)
        staged = mutable.require()
        if staged.identity != temporary_identity:
            return HeldResult(
                error=HeldFsError("IDENTITY_CHANGED", "held filesystem identity changed")
            )

        if expected_identity is None:
            linked = filesystem.link(staged, parent, leaf)
            if not linked.ok:
                return publication_error(linked.error)
            removed = filesystem.unlink(staged)
            if not removed.ok:
                # A transient unlink refusal after the no-replace link is an
                # ambiguous outcome unless we finish cleanup or reverse the
                # just-installed destination. Retry once on the same retained
                # name; no pathname reopen is allowed here.
                removed = filesystem.unlink(staged)
            if not removed.ok:
                rollback = filesystem.file(parent, leaf, access="mutate")
                if not rollback.ok:
                    return publication_error(
                        HeldFsError(
                            "ROLLBACK_INCOMPLETE",
                            "create publication could not be reconciled",
                        )
                    )
                with rollback.require() as installed:
                    if not _same_file_identity(installed.identity, staged.identity):
                        return publication_error(
                            HeldFsError(
                                "ROLLBACK_INCOMPLETE",
                                "create publication identity changed",
                            )
                        )
                    reversed_result = filesystem.unlink(installed)
                    if not reversed_result.ok:
                        return publication_error(
                            HeldFsError(
                                "ROLLBACK_INCOMPLETE",
                                "create publication rollback was refused",
                            )
                        )
                return publication_error(removed.error)
        else:
            if expected_sha256 is not None:
                assert existing is not None
                observed = filesystem.read(existing)
                if (
                    not observed.ok
                    or hashlib.sha256(observed.require()).hexdigest()
                    != expected_sha256
                ):
                    return HeldResult(
                        error=HeldFsError(
                            "IDENTITY_CHANGED", "held filesystem content changed"
                        )
                    )
            replaced = filesystem.rename(staged, parent, leaf, replace=True)
            if not replaced.ok:
                return publication_error(replaced.error)

        published = filesystem.file(parent, leaf)
        if not published.ok:
            return HeldResult(error=published.error)
        with published.require() as installed:
            if installed.identity.link_count != 1:
                return HeldResult(
                    error=HeldFsError("IDENTITY_CHANGED", "published identity is ambiguous")
                )
            return HeldResult(value=installed.identity)
    finally:
        if existing is not None:
            existing.close()
        if staged is not None:
            staged.close()
        cleanup = filesystem.file(parent, temporary, access="mutate")
        if cleanup.ok:
            with cleanup.require() as residue:
                if temporary_identity is not None and residue.identity == temporary_identity:
                    filesystem.unlink(residue)


def publish_sqlite_identities(
    filesystem: HeldFilesystem,
    parent: HeldDirectory,
    primary: str,
    publish: Callable[[dict[str, StableIdentity]], None],
) -> HeldResult[None]:
    """Publish reachable SQLite family identities while caller coordination is held.

    The adapter does not acquire coordination or own a store.  It first reads every
    handle-derived identity and invokes ``publish`` only after primary, WAL, and SHM
    are all reachable, so a caller can publish the complete family before releasing
    its existing cooperative fence.
    """
    names = (primary, f"{primary}-wal", f"{primary}-shm")
    identities: dict[str, StableIdentity] = {}
    for name in names:
        result = filesystem.file(parent, name)
        if not result.ok:
            return HeldResult(error=result.error)
        with result.require() as file:
            identities[name] = file.identity
    try:
        publish(identities)
    except Exception:  # noqa: BLE001 - caller publication must not escape the closed result.
        return HeldResult(error=HeldFsError("PUBLISH_REFUSED", "identity publication was refused"))
    return HeldResult(value=None)
