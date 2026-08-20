"""Closed, handle-relative filesystem primitives for cooperating subsystems.

This module deliberately exposes no path-based descendant operation.  A caller
first acquires a root anchor, then works through retained parent handles.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

T = TypeVar("T")
_MISSING = object()


@dataclass(slots=True)
class HeldFsError(Exception):
    """A typed, content-free refusal from a held filesystem operation."""

    code: str
    detail: str

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


@dataclass(frozen=True, slots=True)
class SagaRecord:
    """One deterministic recursive enumeration effect/observation record."""

    relative_path: str
    identity: StableIdentity


class HeldDirectory:
    """A retained directory handle.  Instances are created by ``parent`` only."""

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

    def parent(self, relative: str, *, create: bool = False) -> HeldResult[HeldDirectory]:
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

    def read(self, file: HeldFile) -> HeldResult[bytes]:
        raise NotImplementedError

    def write(self, file: HeldFile, data: bytes) -> HeldResult[None]:
        raise NotImplementedError

    def rename(
        self,
        source: HeldFile,
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

    def copy(
        self,
        source: HeldFile,
        destination_parent: HeldDirectory,
        destination_leaf: str,
    ) -> HeldResult[None]:
        raise NotImplementedError

    def enumerate(self, parent: HeldDirectory) -> HeldResult[tuple[SagaRecord, ...]]:
        raise NotImplementedError


def _backend():
    if os.name == "nt":  # pragma: no cover - exercised by the Windows CI gate
        from . import _held_fs_windows as backend
    else:
        from . import _held_fs_posix as backend
    return backend


def probe(root: Path) -> Capabilities:
    """Probe the actual target filesystem; no failed probe gets a fallback."""
    return _backend().probe(Path(root))


def acquire(root: Path) -> HeldResult[HeldFilesystem]:
    """Acquire a root anchor or return a content-free capability/IO refusal."""
    return _backend().acquire(Path(root))


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
