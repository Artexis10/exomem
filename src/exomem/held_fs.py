"""Closed, handle-relative filesystem primitives for cooperating subsystems.

This module deliberately exposes no path-based descendant operation.  A caller
first acquires a root anchor, then works through retained parent handles.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import sys
import threading
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Set
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Generic, Literal, TypeVar

T = TypeVar("T")
_MISSING = object()
PUBLISH_TEMP_PREFIX = ".exomem-held-publish-"
# nosemgrep: ep-word-set -- the closed `file()` access modes that every backend implements.
FILE_ACCESS_MODES = frozenset({"read", "write", "mutate", "identity"})
# The name format that publish_temporary_name creates: the prefix and 32 hexadecimal digits.
_PUBLISH_TEMP_RE = re.compile(rf"{re.escape(PUBLISH_TEMP_PREFIX)}[0-9a-f]{{32}}", re.ASCII)
_CAPABILITY_CACHE: dict[tuple[int, str, int, int], Capabilities] = {}
_CAPABILITY_CACHE_LOCK = threading.Lock()
_INSTALLATION_SEAL = object()
_RETIREMENT_SEAL = object()
_NAME_RETIREMENT_SEAL = object()


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


class CustodyAcknowledgementPending(RuntimeError):
    """The owner retained its reservation because it could not persist a release acknowledgement."""


def vault_name_key(name: str, *, form: str = "NFC") -> str:
    """The identity key of one vault path part: normalise, then casefold.

    Path identity belongs to the vault, not the host filesystem. Two names with
    one key cannot sit in one folder, because Windows and default macOS would
    hold them as a single entry. This is the only spelling of that rule;
    `form="NFKC"` serves the looser audit and vocabulary comparisons.
    """
    return unicodedata.normalize(form, name).casefold()


def split_relative(path: str) -> tuple[str, str]:
    """A vault-relative path's parent and leaf, the parent joined with forward slashes.

    Held paths are POSIX-style on every platform. `str(Path(...))` would join the parent with
    backslashes on Windows, which the held filesystem refuses as an unsafe path.
    """
    relative = PurePosixPath(path)
    return relative.parent.as_posix(), relative.name


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
    no_replace_rename: bool
    reason: str = ""

    @classmethod
    def disabled(cls, reason: str) -> Capabilities:
        return cls(False, False, False, False, False, False, reason)


Capability = Literal["relative_operations", "no_follow", "stable_identity", "same_device_rename",
                     "hard_link", "no_replace_rename"]
# Hard links have always been optional for the default vault and state-root acquisition.
# nosemgrep: ep-word-set -- the fields of the held-filesystem capability contract above.
_DEFAULT_REQUIREMENTS: frozenset[Capability] = frozenset({
    "relative_operations", "no_follow", "stable_identity", "same_device_rename", "no_replace_rename",
})


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


@dataclass(frozen=True, slots=True)
class InstallationWitness:
    """A native installation observed while its original allocation remains held."""

    root_identity: StableIdentity
    parent_identity: StableIdentity
    leaf: str
    before: FileVersion
    after: FileVersion
    _seal: object = field(repr=False, compare=False)

    def authentic(self) -> bool:
        return self._seal is _INSTALLATION_SEAL


@dataclass(frozen=True, slots=True)
class FileRetirementState:
    """Native namespace state observed from the original retained allocation."""

    version: FileVersion
    delete_pending: bool | None = None

    @property
    def identity(self) -> StableIdentity:
        return self.version.identity


@dataclass(frozen=True, slots=True)
class RetiringFile:
    file: HeldFile
    expected: FileVersion


@dataclass(frozen=True, slots=True)
class NameRetirementReceipt:
    """A backend's durable replacement effect, captured before publisher callbacks."""

    root_identity: StableIdentity
    parent_identity: StableIdentity
    leaf: str
    before: FileVersion
    observed: FileRetirementState
    _seal: object = field(repr=False, compare=False)

    def authentic(self) -> bool:
        return self._seal is _NAME_RETIREMENT_SEAL


@dataclass(frozen=True, slots=True)
class RetirementWitness:
    """Durable name retirement, delivered after this publisher closes the old handle."""

    root_identity: StableIdentity
    parent_identity: StableIdentity
    leaf: str
    before: FileVersion
    observed: FileRetirementState
    _seal: object = field(repr=False, compare=False)

    def authentic(self) -> bool:
        return self._seal is _RETIREMENT_SEAL


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

    def retirement_state(self) -> HeldResult[FileRetirementState]:
        return HeldResult(error=HeldFsError("UNSUPPORTED", "held retirement observation is unavailable"))


@dataclass(frozen=True, slots=True)
class TemporaryCustody:
    """Trusted owner hooks: reserve before create, bind before bytes, acknowledge actual closure.

    ``bind`` borrows the file and must not mutate or close it. A failed release leaves the
    owner's durable reservation outstanding. ``release`` reports that case with
    ``CustodyAcknowledgementPending``; publication success does not promise collection.
    """

    reserve: Callable[[str], None]
    bind: Callable[[str, HeldFile], None]
    # This closed protocol names the native facts that can discharge a temporary's copy obligation.
    release: Callable[[str, StableIdentity | None, Literal["not_written", "removed", "installed"]], None]


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

        `access="flush"` lets the caller publish a child and flush this
        directory, including the held root at `.`.
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
        """Hold the regular file `leaf` under `parent`.

        `access="identity"` holds the file only to observe and compare its
        identity: such a handle cannot be created, read, copied, written or
        renamed. On POSIX it opens no descriptor that can carry a record lock.
        Closing any ordinary descriptor for a file drops every POSIX lock that
        this process holds on the file, including the locks of its own open
        SQLite connections.
        """
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
        retiring: RetiringFile | None = None,
    ) -> HeldResult[NameRetirementReceipt | None]:
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


#: The name prefix of every entry a backend's capability probe creates in the
#: root it tests.
PROBE_ENTRY_PREFIX = ".exomem-held-probe-"
#: The probe's own entry names: the prefix, its 32-hex token and an optional role suffix such
#: as `-alias`. The probe briefly leaves symlink aliases in its root, which can lie inside the
#: Knowledge Base, so vault scans skip these names and `reserved_paths` refuses writes under them.
PROBE_ENTRY_RE = re.compile(rf"{re.escape(PROBE_ENTRY_PREFIX)}[0-9a-f]{{32}}(?:-[a-z]+)*", re.ASCII)


def is_probe_entry(name: str) -> bool:
    """Whether a directory entry name is one that a capability probe creates."""
    return PROBE_ENTRY_RE.fullmatch(name) is not None


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


def acquire(root: Path, *, required: Set[Capability] = _DEFAULT_REQUIREMENTS) -> HeldResult[HeldFilesystem]:
    """Acquire a root anchor with the requested capabilities, defaulting to vault and state-root requirements."""
    root = Path(root)
    capability = probe(root)
    if any(not getattr(capability, name) for name in required):
        return HeldResult(error=HeldFsError("CAPABILITY_UNAVAILABLE", "required filesystem primitives are unavailable"))
    return _backend().acquire(root, capability=capability)


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


def _published_identity(
    filesystem: HeldFilesystem, parent: HeldDirectory, leaf: str,
    staged: HeldFile, before: FileVersion,
    after_install: Callable[[InstallationWitness], None] | None,
) -> HeldResult[StableIdentity]:
    published = filesystem.file(parent, leaf)
    if not published.ok:
        return HeldResult(error=published.error)
    with published.require() as installed:
        after = installed.version()
        if (after.identity.link_count != 1 or not _same_file_identity(after.identity, staged.identity)
                or (after.size, after.mtime_ns) != (before.size, before.mtime_ns)):
            return HeldResult(
                error=HeldFsError("IDENTITY_CHANGED", "published identity is ambiguous")
            )
        if after_install is not None:
            after_install(InstallationWitness(
                filesystem.root_identity, parent.identity, leaf, before, after, _INSTALLATION_SEAL,
            ))
        return HeldResult(value=after.identity)


def _retirement_before(retiring: RetiringFile | None) -> FileVersion | None:
    if retiring is not None:
        try:
            if retiring.file.version() == retiring.expected:
                return retiring.expected
        except (OSError, HeldFsError):
            pass
    return None


def _name_retirement_receipt(
    filesystem: HeldFilesystem, parent: HeldDirectory, leaf: str,
    retiring: RetiringFile | None, before: FileVersion | None,
) -> NameRetirementReceipt | None:
    """Called only inside the native backend, after its replacement and parent barriers."""
    if retiring is None or before is None:
        return None
    try:
        if (retiring.file.filesystem is not filesystem or retiring.file.name != leaf
                or not _same_file_identity(retiring.file.parent_identity, parent.identity)):
            return None
        state = retiring.file.retirement_state()
        if state.ok:
            current = state.require()
            if (_same_file_identity(current.identity, before.identity) and current.identity.link_count == 0
                    and current.delete_pending is not False
                    and (filesystem.name_changes_flush_parents or filesystem.flush_directory(parent).ok)):
                return NameRetirementReceipt(filesystem.root_identity, parent.identity, leaf, before, current,
                                             _NAME_RETIREMENT_SEAL)
    except (OSError, HeldFsError):
        pass
    return None


def _close_replaced(
    filesystem: HeldFilesystem, parent: HeldDirectory, leaf: str,
    existing: HeldFile | None, before: FileVersion | None, receipt: NameRetirementReceipt | None,
    after_retire: Callable[[RetirementWitness], None] | None,
) -> None:
    if existing is None:
        return
    observed = None
    try:
        if (after_retire is not None and before is not None and receipt is not None and receipt.authentic()
                and receipt.before == before and receipt.root_identity == filesystem.root_identity
                and receipt.parent_identity == parent.identity and receipt.leaf == leaf):
            state = existing.retirement_state()
            if state.ok and state.require() == receipt.observed:
                observed = receipt.observed
    except (OSError, HeldFsError):
        # Missing native or durability evidence keeps the old copy unresolved without blocking publication.
        pass
    finally:
        existing.close()
    if observed is not None:
        assert before is not None and after_retire is not None
        witness = RetirementWitness(filesystem.root_identity, parent.identity, leaf, before, observed, _RETIREMENT_SEAL)
        try:
            after_retire(witness)
        except CustodyAcknowledgementPending:
            # The old allocation retired; its canonical obligation remains open until the owner persists acknowledgement.
            pass


def install_temporary(
    filesystem: HeldFilesystem,
    parent: HeldDirectory,
    temporary: str,
    leaf: str,
    *,
    expected_identity: StableIdentity,
    expected_version: FileVersion | None = None,
    expected_destination: FileVersion | None = None,
    before_install: Callable[[], None] | None = None,
    after_install: Callable[[InstallationWitness], None] | None = None,
    after_retire: Callable[[RetirementWitness], None] | None = None,
) -> HeldResult[StableIdentity]:
    """Install a temporary that the caller already wrote and verified, without copying its bytes.

    `temporary` must still be the file of `expected_identity`, so a swapped temporary is
    refused. The caller owns the temporary until this succeeds; afterwards its name is gone.
    A replacement requires the destination's exact retained version. The caller holds its
    writer boundary and binds a completed content verification to both supplied versions.
    ``before_install`` rechecks the owner's publication right at the native effect boundary.
    ``after_install`` receives the destination version while the original allocation remains held.
    ``after_retire`` reports durable retirement after the old destination handle closes; missing proof leaves its obligation open.
    """

    def publication_error(error: HeldFsError | None) -> HeldResult[StableIdentity]:
        bounded = error or HeldFsError("IO_REFUSED", "publish was refused")
        bounded.source_leaf = temporary
        bounded.destination_leaf = leaf
        return HeldResult(error=bounded)

    if temporary == leaf:
        return HeldResult(error=HeldFsError("INVALID_INPUT", "temporary and destination must differ"))
    if expected_version is not None and expected_version.identity != expected_identity:
        return HeldResult(error=HeldFsError("INVALID_INPUT", "temporary version names a different identity"))
    existing: HeldFile | None = None
    existing_before: FileVersion | None = None
    retirement = None
    try:
        current = filesystem.file(parent, leaf)
        if expected_destination is None:
            if current.ok:
                current.require().close()
                return HeldResult(error=HeldFsError("DESTINATION_EXISTS", "destination already exists"))
            if current.error is None or current.error.code != "MISSING":
                return HeldResult(error=current.error or HeldFsError("IO_REFUSED", "publish refused"))
        else:
            if expected_version is None:
                if current.ok:
                    current.require().close()
                return HeldResult(error=HeldFsError("INVALID_INPUT", "replacement requires a verified temporary version"))
            if not current.ok:
                return HeldResult(error=current.error)
            existing = current.require()
            if existing.version() != expected_destination or existing.identity.link_count != 1:
                return HeldResult(error=HeldFsError("IDENTITY_CHANGED", "destination version changed"))
        mutable = filesystem.file(parent, temporary, access="mutate")
        if not mutable.ok:
            return HeldResult(error=mutable.error)
        with mutable.require() as staged:
            if staged.identity != expected_identity or (expected_version is not None and staged.version() != expected_version):
                return HeldResult(error=HeldFsError("IDENTITY_CHANGED", "temporary version changed"))
            if expected_destination is None:
                if before_install is not None:
                    before_install()
                before = staged.version()
                if expected_version is not None and before != expected_version:
                    return HeldResult(error=HeldFsError("IDENTITY_CHANGED", "temporary version changed"))
                failure = _link_without_replace(filesystem, parent, staged, leaf, publication_error)
                if failure is not None:
                    return failure
            else:
                assert existing is not None
                if existing.version() != expected_destination:
                    return HeldResult(error=HeldFsError("IDENTITY_CHANGED", "destination version changed"))
                named = filesystem.file(parent, leaf)
                if not named.ok:
                    return HeldResult(error=named.error)
                with named.require() as destination:
                    if destination.version() != expected_destination or staged.identity.link_count != 1:
                        return HeldResult(error=HeldFsError("IDENTITY_CHANGED", "destination version changed"))
                if before_install is not None:
                    before_install()
                existing_before = existing.version()
                if existing_before != expected_destination:
                    return HeldResult(error=HeldFsError("IDENTITY_CHANGED", "destination version changed"))
                before = staged.version()
                if before != expected_version:
                    return HeldResult(error=HeldFsError("IDENTITY_CHANGED", "temporary version changed"))
                replaced = filesystem.rename(staged, parent, leaf, replace=True,
                    **({"retiring": RetiringFile(existing, existing_before)} if after_retire is not None else {}))
                if not replaced.ok:
                    return publication_error(replaced.error)
                retirement = replaced.value
            return _published_identity(filesystem, parent, leaf, staged, before, after_install)
    except OSError as error:
        code = "IDENTITY_CHANGED" if error.errno == errno.ESTALE else "IO_REFUSED"
        return publication_error(HeldFsError(code, "temporary installation was refused", cause=error))
    finally:
        _close_replaced(filesystem, parent, leaf, existing, existing_before, retirement, after_retire)


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
    custody: TemporaryCustody | None = None,
    before_install: Callable[[], None] | None = None,
    after_install: Callable[[InstallationWitness], None] | None = None,
    after_retire: Callable[[RetirementWitness], None] | None = None,
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
    ``before_install`` rechecks the owner's publication right after copying, before native installation.
    ``after_install`` receives the destination version while the original allocation remains held.
    ``after_retire`` reports durable retirement after the old destination handle closes; missing proof leaves its obligation open.
    """

    if not isinstance(data, bytes | Chunks):
        return HeldResult(error=HeldFsError("INVALID_INPUT", "data must be bytes"))
    existing: HeldFile | None = None
    existing_before: FileVersion | None = None
    staged: HeldFile | None = None
    writable: HeldFile | None = None
    temporary = publish_temporary_name()
    temporary_identity: StableIdentity | None = None
    custody_reserved = False
    copy_started = False
    installed = False
    retirement = None

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

        if custody is not None:
            custody.reserve(temporary)
            custody_reserved = True
        created = filesystem.file(
            parent,
            temporary,
            access="write",
            create=True,
            exclusive=True,
        )
        if not created.ok:
            return HeldResult(error=created.error)
        writable = created.require()
        # Retain the allocation through cleanup, so a reopened name cannot recycle its identity.
        temporary_identity = writable.identity
        if custody is not None:
            custody.bind(temporary, writable)
        copy_started = True
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
            if before_install is not None:
                before_install()
            before = staged.version()
            failure = _link_without_replace(filesystem, parent, staged, leaf, publication_error)
            if failure is not None:
                return failure
            installed = True
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
            if before_install is not None:
                before_install()
            assert existing is not None
            existing_before = existing.version() if after_retire is not None else None
            before = staged.version()
            replaced = filesystem.rename(staged, parent, leaf, replace=True,
                **({"retiring": RetiringFile(existing, existing_before)} if after_retire is not None else {}))
            if not replaced.ok:
                return publication_error(replaced.error)
            installed = True
            retirement = replaced.value

        return _published_identity(filesystem, parent, leaf, staged, before, after_install)
    finally:
        try:
            _close_replaced(filesystem, parent, leaf, existing, existing_before, retirement, after_retire)
        finally:
            try:
                released = "installed" if installed else "not_written" if not copy_started else None
                cleanup = filesystem.file(parent, temporary, access="mutate")
                if cleanup.ok:
                    with cleanup.require() as residue:
                        if temporary_identity is not None and residue.identity == temporary_identity:
                            if filesystem.unlink(residue).ok:
                                released = "removed"
                if custody is not None and custody_reserved and released is not None:
                    try:
                        custody.release(temporary, temporary_identity, released)
                    except CustodyAcknowledgementPending:
                        # Acknowledgement failure preserves the durable reservation for owner reconciliation.
                        pass
            finally:
                if staged is not None:
                    staged.close()
                if writable is not None:
                    writable.close()


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
        # The caller's connection is live: an ordinary descriptor would drop its locks on close.
        result = filesystem.file(parent, name, access="identity")
        if not result.ok:
            return HeldResult(error=result.error)
        with result.require() as file:
            identities[name] = file.identity
    try:
        publish(identities)
    except Exception:  # noqa: BLE001 - caller publication must not escape the closed result.
        return HeldResult(error=HeldFsError("PUBLISH_REFUSED", "identity publication was refused"))
    return HeldResult(value=None)
