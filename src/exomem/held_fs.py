"""Closed, handle-relative filesystem primitives for cooperating subsystems.

This module deliberately exposes no path-based descendant operation.  A caller
first acquires a root anchor, then works through retained parent handles.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import sys
import threading
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generic, TypeVar

T = TypeVar("T")
_MISSING = object()
PUBLISH_TEMP_PREFIX = ".exomem-held-publish-"
# The name format that publish_temporary_name creates: the prefix and 32 hexadecimal digits.
_PUBLISH_TEMP_RE = re.compile(rf"{re.escape(PUBLISH_TEMP_PREFIX)}[0-9a-f]{{32}}", re.ASCII)
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
    #: On PATH_VARIANT_EXISTS, the sibling's spelling, for the caller to name.
    existing_leaf: str | None = field(default=None, repr=False, compare=False)

    def __str__(self) -> str:
        return f"{self.code}: {self.detail}"


def vault_name_key(name: str, *, form: str = "NFC") -> str:
    """The identity key of one vault path part: normalise, then casefold.

    Path identity belongs to the vault, not the host filesystem. Two names with
    one key cannot sit in one folder, because Windows and default macOS would
    hold them as a single entry. This is the only spelling of that rule;
    `form="NFKC"` serves the looser audit and vocabulary comparisons.
    """
    return unicodedata.normalize(form, name).casefold()


def name_variants(names: Iterable[str], name: str) -> list[str]:
    """The names in `names` that share `name`'s `vault_name_key` but not its spelling, sorted."""
    key = vault_name_key(name)
    return sorted(other for other in names if other != name and vault_name_key(other) == key)


@dataclass(frozen=True, slots=True)
class Chunks:
    """Bytes a held write copies a part at a time, so a large file is never whole in memory.

    Each write calls `open` for a fresh pass, so a retried publication copies the same bytes.
    """

    open: Callable[[], Iterable[bytes]]


def write_parts(data: bytes | Chunks) -> Iterable[bytes]:
    """The parts a held write copies, in order."""
    return (data,) if isinstance(data, bytes) else data.open()


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


@dataclass(frozen=True, slots=True)
class FileVersion:
    """A held identity and platform change witness for ordinary concurrent writes."""

    identity: StableIdentity
    size: int
    mtime_ns: int
    change_time_ns: int


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

    def version(self) -> FileVersion:
        raise NotImplementedError


class HeldFilesystem:
    """A retained vault-root anchor with handle-relative leaf operations."""

    # Whether a successful ``rename`` or ``unlink_directory`` flushed every parent directory it changed.
    name_changes_flush_parents = False

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
        scratch: bool = False,
    ) -> HeldResult[HeldDirectory]:
        """Hold the directory at `relative`, creating missing parts when `create` is set.

        A folder that this call creates is refused with PATH_VARIANT_EXISTS when
        its parent already holds a name with the same `vault_name_key`, so no
        creator can split a folder by case or Unicode form. `scratch=True` marks
        a randomly named working folder that the caller removes again: it cannot
        collide, so it skips the parent listing that the check costs.
        """
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

    def read(self, file: HeldFile, *, max_bytes: int | None = None) -> HeldResult[bytes]:
        raise NotImplementedError

    def write(self, file: HeldFile, data: bytes | Chunks) -> HeldResult[None]:
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
        *, max_bytes: int | None = None, deadline: float | None = None,
    ) -> HeldResult[None]:
        raise NotImplementedError

    def children(
        self,
        parent: HeldDirectory,
        *,
        name_filter: Callable[[str], bool] | None = None,
    ) -> HeldResult[tuple[SagaRecord, ...]]:
        """Return stable children, omitting unsafe alias objects.

        Filter names before acquiring identities when a caller supplies a filter.
        """

        raise NotImplementedError

    def iter_names(self, parent: HeldDirectory) -> Iterator[str]:
        """Stream unsorted immediate names, including aliases for caller checking.

        This read-only iterator raises HeldFsError on acquisition/iteration
        failure. Close it before releasing its retained directory.
        """
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


@dataclass(frozen=True, slots=True)
class PlatformSupport:
    """Whether this host has a held-filesystem backend, and why not when it has none."""

    supported: bool
    reason: str = ""


def platform_support() -> PlatformSupport:
    """Answer the host question without a root, so a caller can refuse early.

    `probe` answers a question about one filesystem and can differ per vault.
    This answers a question about the build: on a platform with no backend, no
    vault will ever work, so the only honest response is to say so once rather
    than let every reserved-path acquisition fail separately and describe
    itself as an unavailable route.
    """
    backend = _backend()
    if backend.platform_supported():
        return PlatformSupport(True)
    return PlatformSupport(
        False,
        f"exomem has no held-filesystem backend for {sys.platform!r}; "
        "Linux and Windows are the platforms it can serve today",
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


def publish_temporary_name() -> str:
    """A fresh name in the reserved publish namespace, which the watcher and scans skip."""
    return f"{PUBLISH_TEMP_PREFIX}{secrets.token_hex(16)}"


def is_publish_temporary(name: str) -> bool:
    """Whether `name` has the exact form that `publish_temporary_name` creates."""
    return _PUBLISH_TEMP_RE.fullmatch(name) is not None


def _link_without_replace(
    filesystem: HeldFilesystem,
    parent: HeldDirectory,
    staged: HeldFile,
    leaf: str,
    publication_error: Callable[[HeldFsError | None], HeldResult[StableIdentity]],
) -> HeldResult[StableIdentity] | None:
    """Link `staged` to `leaf` with no replace and drop its temporary name; None on success."""
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
    if removed.ok:
        return None
    rollback = filesystem.file(parent, leaf, access="mutate")
    if not rollback.ok:
        return publication_error(
            HeldFsError("ROLLBACK_INCOMPLETE", "create publication could not be reconciled")
        )
    with rollback.require() as installed:
        if not _same_file_identity(installed.identity, staged.identity):
            return publication_error(
                HeldFsError("ROLLBACK_INCOMPLETE", "create publication identity changed")
            )
        reversed_result = filesystem.unlink(installed)
        if not reversed_result.ok:
            return publication_error(
                HeldFsError("ROLLBACK_INCOMPLETE", "create publication rollback was refused")
            )
    return publication_error(removed.error)


def _published_identity(filesystem: HeldFilesystem, parent: HeldDirectory, leaf: str) -> HeldResult[StableIdentity]:
    published = filesystem.file(parent, leaf)
    if not published.ok:
        return HeldResult(error=published.error)
    with published.require() as installed:
        if installed.identity.link_count != 1:
            return HeldResult(
                error=HeldFsError("IDENTITY_CHANGED", "published identity is ambiguous")
            )
        return HeldResult(value=installed.identity)


def install_temporary(
    filesystem: HeldFilesystem,
    parent: HeldDirectory,
    temporary: str,
    leaf: str,
    *,
    expected_identity: StableIdentity,
) -> HeldResult[StableIdentity]:
    """Link a temporary that the caller already wrote and verified into place, with no replace.

    `temporary` must still be the file of `expected_identity`, so a swapped temporary is
    refused. The caller owns the temporary until this succeeds; afterwards its name is gone.
    """

    def publication_error(error: HeldFsError | None) -> HeldResult[StableIdentity]:
        bounded = error or HeldFsError("IO_REFUSED", "publish was refused")
        bounded.source_leaf = temporary
        bounded.destination_leaf = leaf
        return HeldResult(error=bounded)

    current = filesystem.file(parent, leaf)
    if current.ok:
        current.require().close()
        return HeldResult(error=HeldFsError("DESTINATION_EXISTS", "destination already exists"))
    if current.error is None or current.error.code != "MISSING":
        return HeldResult(error=current.error or HeldFsError("IO_REFUSED", "publish refused"))
    mutable = filesystem.file(parent, temporary, access="mutate")
    if not mutable.ok:
        return HeldResult(error=mutable.error)
    with mutable.require() as staged:
        if staged.identity != expected_identity:
            return HeldResult(error=HeldFsError("IDENTITY_CHANGED", "held filesystem identity changed"))
        failure = _link_without_replace(filesystem, parent, staged, leaf, publication_error)
        if failure is not None:
            return failure
    return _published_identity(filesystem, parent, leaf)


def publish_bytes(
    filesystem: HeldFilesystem,
    parent: HeldDirectory,
    leaf: str,
    data: bytes | Chunks,
    *,
    expected_identity: StableIdentity | None = None,
    expected_sha256: str | None = None,
    validate_existing: Callable[[HeldFile], None] | None = None,
    prepare: Callable[[HeldFile], None] | None = None,
) -> HeldResult[StableIdentity]:
    """Atomically publish bytes, or `Chunks` of them, under one held parent.

    A read error from a `Chunks` source returns as the publish's own refusal; any other
    exception the source raises, such as a path guard's, propagates after the temporary
    is removed.

    With no expected identity the publish is create-only and uses a no-replace
    hard-link installation. With an expected identity, that exact destination
    stays held while a native relative replacement installs the staged bytes.
    ``validate_existing`` checks that borrowed handle before and after staging;
    the callback must neither close nor mutate it.
    """

    if not isinstance(data, bytes | Chunks):
        return HeldResult(error=HeldFsError("INVALID_INPUT", "data must be bytes"))
    existing: HeldFile | None = None
    staged: HeldFile | None = None
    temporary = publish_temporary_name()
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
            if validate_existing is not None:
                validate_existing(existing)

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
            # Known before the copy, so a copy that stops part-way still removes the temporary.
            temporary_identity = writable.identity
            written = filesystem.write(writable, data)
            if not written.ok:
                return HeldResult(error=written.error)
            if prepare is not None:
                prepare(writable)

        mutable = filesystem.file(parent, temporary, access="mutate")
        if not mutable.ok:
            return HeldResult(error=mutable.error)
        staged = mutable.require()
        if staged.identity != temporary_identity:
            return HeldResult(
                error=HeldFsError("IDENTITY_CHANGED", "held filesystem identity changed")
            )

        if expected_identity is None:
            failure = _link_without_replace(filesystem, parent, staged, leaf, publication_error)
            if failure is not None:
                return failure
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
            if validate_existing is not None:
                assert existing is not None
                validate_existing(existing)
            replaced = filesystem.rename(staged, parent, leaf, replace=True)
            if not replaced.ok:
                return publication_error(replaced.error)

        return _published_identity(filesystem, parent, leaf)
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
