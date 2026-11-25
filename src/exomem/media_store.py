"""Encrypted, version-pinned originals; the volume is a verified local cache.

A store is selected only by explicit configuration: the media edge for Cloud cells,
and a filesystem adapter for a local disk or a mounted NAS. No store is selected by default.
Jobs share the media ledger; protection records outlive those jobs.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import logging
import math
import os
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import ExitStack, closing, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from itertools import chain
from pathlib import Path, PurePosixPath
from queue import Empty, Queue
from typing import TYPE_CHECKING, BinaryIO, Protocol, TypeVar, cast

from . import (
    held_fs,
    media_allowance,
    media_capacity,
    media_drain_record,
    originals,
    reserved_paths,
    trash_metadata,
)
from .kbdir import managed_root
from .media_jobs import (
    OFFLOAD_MEDIA_TYPE,
    STATUS_JOB_LIMIT,
    MediaJob,
    MediaJobStore,
    holder_alive,
    process_start,
)
from .originals import OriginalSnapshot, StoredObject, original_state

if TYPE_CHECKING:
    from .media_protection import ProtectionPolicy
    from .media_protection_budget import Transfer
    from .vault import PlannedWrite

log = logging.getLogger(__name__)


class MediaStoreError(RuntimeError):
    def __init__(self, code: str, reason: str):
        self.code = code
        self.reason = reason
        super().__init__(f"{code}: {reason}")


def register_path_custody() -> None:
    """Use existing path receipts; eviction alone verifies this optional derived inventory."""
    from . import freshness, media_bindings

    freshness.register_custody_seam(freshness.CustodySeam(
        name="media_store",
        apply=lambda root, changed, deleted, digests: media_bindings.repair_paths(root, (*changed, *deleted)),
        # Missing binding facts retain bytes and must never gate an unrelated read.
        invalidate_scope=lambda root, scope: media_bindings.external_change(root),
    ))


# The refusal that the classification rule records for an original that stays on the volume (D12).
RESIDENT_CODE = "MEDIA_ORIGINAL_RESIDENT"
# An original whose plaintext exceeds the whole hydration capacity; waiting never clears it (D3, M4).
HYDRATION_TOO_LARGE_CODE = "MEDIA_HYDRATION_TOO_LARGE"
# The hydration failures that return a job to the queue: this module's own transport and capacity codes.
STORE_BUSY_CODE = "MEDIA_STORE_BUSY"
RETRYABLE_HYDRATION_CODES = frozenset({"MEDIA_STORE_UNAVAILABLE", STORE_BUSY_CODE})


def _resident(vault: Path, snapshot: OriginalSnapshot, config: dict[str, object]) -> bool:
    bounds = media_capacity.bounds_for(config, vault)
    assert snapshot.binary_size is not None
    return bounds is not None and bounds.classify(snapshot.binary_size) == media_capacity.RESIDENT


class ObjectTooLarge(MediaStoreError):
    """The store refuses this declared size under the current object bound."""

    def __init__(self, size: int, bound: int):
        super().__init__("MEDIA_OBJECT_TOO_LARGE", f"{size} bytes exceed the per-object bound of {bound}")
        self.size = size


class ReplicaMismatch(MediaStoreError):
    """A store holds the version, and its size or digest is not the one the binding records: damage, never an outage."""

    def __init__(self, failure: str):
        super().__init__("MEDIA_ORIGINAL_UNVERIFIED", "the pinned ciphertext differs")
        self.failure = failure


class ReplicaAbsent(MediaStoreError):
    """The identified store confirms that it does not hold this version."""

    failure = "absent"

    def __init__(self):
        super().__init__("MEDIA_STORE_UNAVAILABLE", "the pinned version is absent from the store")


#: The finding when ciphertext that matches its record is refused by the key: the identity or the recipient is wrong.
KEY_REFUSED = "key_refused"


class KeyRefused(MediaStoreError):
    """The ciphertext is the one the binding records, and the decryption identity refuses it: the key, not the bytes."""

    failure = KEY_REFUSED

    def __init__(self) -> None:
        super().__init__("MEDIA_ORIGINAL_UNVERIFIED", "the decryption identity refuses the ciphertext")


@dataclass(frozen=True, slots=True)
class ObjectHead:
    size: int
    sha256: str
    # A hide or a newer version made the version non-current, so the bucket rule will delete it
    # (collect-released-media D3). It still reads by version until then.
    condemned: bool = False


class Store(Protocol):
    # The declared size and digest let the store refuse before or instead of publishing (D6).
    def upload(self, name: str, stream: BinaryIO, *, size: int, sha256: str) -> str: ...
    def head(self, name: str, version: str) -> ObjectHead | None: ...
    def open(self, name: str, version: str): ...


class _HashProgress:
    def __init__(self) -> None:
        self.size = 0
        self.requested = 0
        self.digest = hashlib.sha256()


class ReplicaCheckPending(Exception):
    """This round ended between chunks of a held filesystem hash."""


class ScheduledRead:
    """Carry one filesystem hash per scheduled store across rounds without keeping a file open."""

    def __init__(self) -> None:
        self.bytes_read: int | None = None
        self._binding: tuple[Path, str, str, held_fs.FileVersion] | None = None
        self._progress: _HashProgress | None = None

    def reset(self) -> None:
        self._binding = self._progress = None

    def hash(self, stream: BinaryIO, binding: tuple[Path, str, str, held_fs.FileVersion],
             deadline: float | None) -> ObjectHead:
        if self._binding != binding or self._progress is None:
            self._binding, self._progress = binding, _HashProgress()
        progress = self._progress
        start = progress.size
        try:
            stream.seek(start)
            return _digest(stream, deadline=deadline, limit=binding[-1].size, progress=progress)
        except reserved_paths.ReservedPathLeafError as error:
            raise ReplicaCheckPending() from error
        finally:
            # An errored read can have moved at most this last chunk.
            self.bytes_read = progress.size - start + progress.requested


def _digest(stream: BinaryIO, *, deadline: float | None = None, limit: int | None = None,
            progress: _HashProgress | None = None) -> ObjectHead:
    """Hash a stream, up to the held length when the caller has a size proof."""
    current = progress if progress is not None else _HashProgress()
    while limit is None or current.size < limit:
        reserved_paths._copy_deadline(deadline)
        remaining = min(1024 * 1024, limit - current.size) if limit is not None else 1024 * 1024
        current.requested = remaining
        chunk = stream.read(remaining)
        current.requested = 0
        if not chunk:
            break
        current.size += len(chunk)
        current.digest.update(chunk)
    return ObjectHead(current.size, current.digest.hexdigest())


def _chunks(stream: BinaryIO):
    while chunk := stream.read(1024 * 1024):
        yield chunk


def _declared_chunks(stream: BinaryIO, size: int, sha256: str):
    """Yield the stream, then refuse it unless it matched its declaration exactly."""
    digest = hashlib.sha256()
    received = 0
    for chunk in _chunks(stream):
        received += len(chunk)
        if received > size:
            break
        digest.update(chunk)
        yield chunk
    if received != size or digest.hexdigest() != sha256:
        raise MediaStoreError("MEDIA_UPLOAD_MISMATCH", "the uploaded bytes differ from the declared size or digest")


class StreamReader:
    def __init__(self, chunks: Iterator[bytes]):
        self._chunks = chunks
        self._buffer = bytearray()

    def read(self, size: int = -1) -> bytes:
        while size < 0 or len(self._buffer) < size:
            chunk = next(self._chunks, None)
            if chunk is None:
                break
            self._buffer.extend(chunk)
        count = len(self._buffer) if size < 0 else size
        block = bytes(self._buffer[:count])
        del self._buffer[:count]
        return block


_ReadValue = TypeVar("_ReadValue")


async def before_deadline(deadline: float, read: Awaitable[_ReadValue]) -> _ReadValue:
    async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
        return await read


#: The adapter name of a local disk or a mounted NAS, and its earlier name, which stays accepted.
FILESYSTEM_ADAPTER = "filesystem"
FILESYSTEM_ADAPTER_ALIAS = "filesystem-test"
B2_REPLICA_ADAPTER = "b2"
#: The file in a filesystem store's root that names the store that the root is. Only `declare_store` writes it.
STORE_MARKER = ".exomem-store"
_MARKER_SCHEMA = 1
_MARKER_MAX_BYTES = 4096


def _read_marker(root: Path) -> dict[str, object] | None:
    """The marker's content; None when the root, the marker or its content is absent, unreadable or not a marker."""
    try:
        descriptor = os.open(root / STORE_MARKER, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as marker:
            if not stat.S_ISREG(os.fstat(marker.fileno()).st_mode):
                return None
            raw = marker.read(_MARKER_MAX_BYTES + 1)
    except OSError:
        return None
    return _parse_marker(raw)


def _parse_marker(raw: bytes) -> dict[str, object] | None:
    """The marker that `raw` holds; None when it is too long, not JSON or not a marker."""
    try:
        value = json.loads(raw) if len(raw) <= _MARKER_MAX_BYTES else None
    except ValueError:
        return None
    if isinstance(value, dict) and value.get("schema") == _MARKER_SCHEMA and isinstance(value.get("store"), str):
        return value
    return None


def _marker_in(filesystem: held_fs.HeldFilesystem) -> dict[str, object] | None:
    """The marker, read through `filesystem`'s own handle on the root; None when that root holds no marker.

    Reading through the held handle ties the answer to the very directory the caller then reads or writes: a root
    that lost its mount is an empty directory behind the handle, and it holds no marker.
    """
    try:
        with filesystem.parent(".").require() as root, filesystem.file(root, STORE_MARKER).require() as marker:
            raw = filesystem.read(marker, max_bytes=_MARKER_MAX_BYTES).require()
    except held_fs.HeldFsError:
        return None
    return _parse_marker(raw)


def read_store_marker(root: Path) -> str | None:
    """The store name that `root`'s marker declares; None when the root, the marker or its content is absent or unreadable."""
    found = _read_marker(root)
    return None if found is None else str(found["store"])


def read_store_id(root: Path) -> str | None:
    """The random id that `root`'s marker carries; None when there is no marker, or an older one that has no id."""
    found = _read_marker(root)
    return found["id"] if found is not None and isinstance(found.get("id"), str) else None


def _store_marker(name: str, store_id: str) -> bytes:
    return json.dumps({"schema": _MARKER_SCHEMA, "store": name, "id": store_id}, separators=(",", ":")).encode("utf-8")


def write_store_marker(root: Path, name: str) -> str:
    """Create the marker that names `root` as store `name`, with a new random id, and return the id; refuses to replace one.

    `declare_store` is the owner's door to it.
    """
    store_id = uuid.uuid4().hex
    descriptor = os.open(root / STORE_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as marker:
        marker.write(_store_marker(name, store_id))
        marker.flush()
        os.fsync(marker.fileno())
    held_fs.flush_directory_path(root).require()
    return store_id


def _hold_store_root(root: Path) -> held_fs.HeldFilesystem:
    return held_fs.acquire(
        root, required={"relative_operations", "no_follow", "stable_identity", "hard_link"},
    ).require()


@dataclass(frozen=True, slots=True)
class _StoredVersionWitness:
    store: FilesystemStore
    root: held_fs.StableIdentity
    capabilities: held_fs.Capabilities
    name: str
    version: str
    file: held_fs.FileVersion
    policy_identity: str | None = None

    def __call__(self) -> None:
        self.store.confirm_versions((self,))


class FilesystemStore:
    """Versioned ciphertext store for a local disk or a mounted NAS, with no deletion API.

    It never creates its root, and it serves only a root whose marker names this store. Without that, a NAS that is
    not mounted would read as an empty store: `head` would answer absent, and the upload would land on the local disk.
    When the owner's declaration recorded the marker's random id (`store_id`), a root whose marker carries another id
    is another store of the same name, and is refused too.
    """

    def __init__(self, root: Path, *, name: str, max_object_bytes: int | None = None, store_id: str | None = None):
        self.root = root
        self.name = name
        self.max_object_bytes = max_object_bytes
        self.store_id = store_id

    def require_identity(self) -> None:
        """Refuse unless the root's marker names this store: a cheap check before work that does not touch the root."""
        self._require_marker(_read_marker(self.root))

    def _require_marker(self, found: dict[str, object] | None) -> None:
        if found is None or found["store"] != self.name or (self.store_id is not None and found.get("id") != self.store_id):
            # Unavailable is never proof that a version is gone: the caller keeps what it recorded.
            raise MediaStoreError("MEDIA_STORE_UNAVAILABLE",
                                  "the store root is missing, or its marker does not name this store")

    @contextmanager
    def _held(self, *, policy_identity: str | None = None):
        """The root's held view, once the marker read through that same view names this store.

        An identity read by path before the view is taken leaves a gap: a volume that goes away in it leaves an empty
        mount point, which `head` would answer as absent and an upload would fill. Here the proof and the work share
        one handle, so a root that is not this store is unavailable, never absent.
        """
        try:
            opened = _hold_store_root(self.root)
        except held_fs.HeldFsError as error:
            raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the store root cannot be held") from error
        with opened as filesystem:
            marker = _marker_in(filesystem)
            self._require_marker(marker)
            if policy_identity is not None and self._transfer_identity(filesystem, marker) != policy_identity:
                raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the policy store identity changed")
            yield filesystem

    @staticmethod
    def _transfer_identity(filesystem: held_fs.HeldFilesystem, marker: dict[str, object] | None) -> str:
        assert marker is not None  # The held store marker has already been checked.
        store_id = marker.get("id")
        root = filesystem.root_identity
        return json.dumps((store_id if isinstance(store_id, str) else None, root.device, root.inode))

    def policy_identity(self) -> str:
        with self._held() as filesystem:
            marker = _marker_in(filesystem)
            self._require_marker(marker)
            return self._transfer_identity(filesystem, marker)

    def _validate_locator(self, name: str, version: str) -> None:
        if not originals.is_sha256(name) or not version or Path(version).name != version or version in {".", ".."}:
            raise ValueError("invalid object locator")

    def upload(self, name: str, stream: BinaryIO, *, size: int, sha256: str) -> str:
        return self._upload_exact(name, stream, size=size, sha256=sha256, version=uuid.uuid4().hex)

    def _upload_exact(
        self, name: str, stream: BinaryIO, *, size: int, sha256: str, version: str,
        policy_identity: str | None = None, before_send: Callable[[], None] | None = None,
    ) -> str:
        self._validate_locator(name, version)
        if not originals.is_sha256(sha256) or size < 0:
            raise ValueError("invalid upload declaration")
        if self.max_object_bytes is not None and size > self.max_object_bytes:
            raise ObjectTooLarge(size, self.max_object_bytes)
        def chunks():
            if before_send is not None:
                before_send()
            yield from _declared_chunks(stream, size, sha256)

        with (self._held(policy_identity=policy_identity) as filesystem,
              filesystem.parent(".", access="flush").require() as root,
              filesystem.parent(name, create=True, access="mutate").require() as parent):
            held_fs.publish_bytes(filesystem, parent, version, held_fs.Chunks(chunks)).require()
            filesystem.flush_directory(parent).require()
            # The version belongs to the object directory, whose name belongs to the store root.
            filesystem.flush_directory(root).require()
        return version

    @contextmanager
    def _open_version(self, filesystem: held_fs.HeldFilesystem, name: str, version: str, *, durable: bool = False):
        parent_result = filesystem.parent(name, access="flush" if durable else "read")
        if parent_result.error is not None and parent_result.error.code == "MISSING":
            raise ReplicaAbsent() from parent_result.error
        with parent_result.require() as parent:
            found = filesystem.file(parent, version)
            if found.error is not None and found.error.code == "MISSING":
                raise ReplicaAbsent() from found.error
            with found.require() as file:
                if durable:
                    with filesystem.parent(".", access="flush").require() as root:
                        filesystem.flush_directory(parent).require()
                        filesystem.flush_directory(root).require()
                yield file

    @contextmanager
    def _held_version(self, name: str, version: str, *, durable: bool = False, policy_identity: str | None = None):
        self._validate_locator(name, version)
        with (self._held(policy_identity=policy_identity) as filesystem,
              self._open_version(filesystem, name, version, durable=durable) as file):
            yield filesystem, file

    @contextmanager
    def open(self, name: str, version: str):
        with self._held_version(name, version) as (_, file):
            with os.fdopen(os.dup(file.descriptor), "rb") as stream:
                yield stream

    def _head_held(
        self, file: held_fs.HeldFile, before: held_fs.FileVersion, name: str, version: str, *,
        expected_size: int | None, deadline: float | None = None, scheduled: ScheduledRead | None = None,
    ) -> ObjectHead:
        if expected_size is not None and before.size != expected_size:
            raise ReplicaMismatch("size_mismatch")
        with os.fdopen(os.dup(file.descriptor), "rb", buffering=0) as stream:
            try:
                if scheduled is None:
                    proof = _digest(stream, deadline=deadline, limit=before.size)
                else:
                    proof = scheduled.hash(stream, (self.root, name, version, before), deadline)
            except ReplicaCheckPending as error:
                if file.version() != before:
                    raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the held replica changed during the read") from error
                raise
            except reserved_paths.ReservedPathLeafError as error:
                raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the replica check exceeded its deadline") from error
        if file.version() != before:
            raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the held replica changed during the read")
        return proof

    def head(self, name: str, version: str, *, expected_size: int | None = None,
             deadline: float | None = None, scheduled: ScheduledRead | None = None) -> ObjectHead | None:
        if scheduled is not None:
            scheduled.bytes_read = 0
        try:
            with self._held_version(name, version) as (_, file):
                return self._head_held(file, file.version(), name, version, expected_size=expected_size,
                                       deadline=deadline, scheduled=scheduled)
        except ReplicaAbsent:
            return None

    @contextmanager
    def hold_verified(
        self, reference: StoredObject, *, policy_identity: str | None = None,
    ) -> Iterator[_StoredVersionWitness]:
        """Keep the checked version open and yield its immutable identity."""
        name, version = reference.object, reference.version
        with (self._held_version(name, version, durable=True, policy_identity=policy_identity) as (filesystem, file),
              filesystem.parent(".").require() as root):
            before = file.version()
            proof = self._head_held(file, before, name, version, expected_size=reference.ciphertext_size)
            if proof.condemned:
                raise ReplicaMismatch("condemned")
            if proof.size != reference.ciphertext_size:
                raise ReplicaMismatch("size_mismatch")
            if proof.sha256 != reference.ciphertext_sha256:
                raise ReplicaMismatch("digest_mismatch")

            yield _StoredVersionWitness(self, root.identity, filesystem.capabilities, name, version, before, policy_identity)

    def confirm_versions(self, witnesses: Sequence[_StoredVersionWitness]) -> None:
        """Confirm the named route and version metadata without reading ciphertext."""
        if not witnesses:
            return
        # A retained root can still reach a lazily unmounted or renamed filesystem.
        # Check the configured name as well, through a separate native root and its own files.
        # The initial check proved the capabilities. A read-only reopen must not probe writes on a replacement root.
        try:
            backend = held_fs._backend()
            capabilities = witnesses[0].capabilities
            opened = backend.acquire(self.root, capability=capabilities)
            with opened.require() as filesystem, filesystem.parent(".").require() as root:
                if any(witness.store is not self or not held_fs._same_file_identity(witness.root, root.identity)
                       for witness in witnesses):
                    raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the checked store moved")
                marker = _marker_in(filesystem)
                self._require_marker(marker)
                policy_identities = {w.policy_identity for w in witnesses if w.policy_identity is not None}
                if policy_identities and policy_identities != {self._transfer_identity(filesystem, marker)}:
                    raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the policy store identity changed")
                for witness in witnesses:
                    with self._open_version(filesystem, witness.name, witness.version) as file:
                        if file.version() != witness.file:
                            raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the checked version moved")
                # A volume can leave its configured name during the file metadata reads.
                with backend.acquire(self.root, capability=capabilities).require() as named:
                    if (not held_fs._same_file_identity(root.identity, named.root_identity)
                            or _marker_in(named) != marker):
                        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the checked store moved")
        except (ReplicaAbsent, held_fs.HeldFsError, OSError) as error:
            raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the checked version is no longer reachable") from error


def _settings(vault: Path) -> dict[str, object]:
    value = originals.configuration(vault)
    if value is None:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "no media store is configured")
    return value


def _adapter(connection: dict[str, object], name: str) -> Store:
    """Build a store from its connection fields. The adapter names are the closed set of adapters this code implements."""
    adapter = connection.get("adapter")
    try:
        if adapter == "media-edge":
            from .media_edge_store import EdgeStore

            return EdgeStore.from_configuration(connection)
        if adapter == B2_REPLICA_ADAPTER:
            from .media_b2_store import B2ReplicaStore

            return B2ReplicaStore.from_configuration(connection)
        root = connection.get("root")
        if adapter not in (FILESYSTEM_ADAPTER, FILESYSTEM_ADAPTER_ALIAS) or not isinstance(root, str):
            raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the media store adapter is unavailable")
        if b"\0" in os.fsencode(root):
            raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the filesystem store root contains a NUL byte")
        bound = connection.get("max_object_bytes")
        return FilesystemStore(Path(root), name=name,
                               max_object_bytes=None if bound is None else int(bound))
    except (TypeError, ValueError, OverflowError) as error:
        # A connection field of the wrong type or form makes this one store unavailable, never the caller's whole pass.
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", f"the store's connection fields are invalid: {error}") from error


def store_for(vault: Path, name: str | None = None) -> Store:
    """The store that serves eviction, or the declared store `name`; a declared store with an incomplete descriptor is unavailable."""
    return _configured_store(vault, _settings(vault), name)


def _configured_store(vault: Path, config: dict[str, object], name: str | None = None) -> Store:
    entry = originals.store_entry(config, name)
    if entry is None:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "no media store of that name is configured")
    backend = _entry_adapter(entry)
    if isinstance(backend, FilesystemStore):
        from . import media_replicas

        backend.store_id = media_replicas.declared_store_id(vault, entry.name)
    return backend


def _entry_adapter(entry: originals.StoreEntry) -> Store:
    if not entry.serves_eviction and not entry.available:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the store's descriptor is incomplete")
    if (refusal := connection_refusal(entry)) is not None:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", refusal)
    return _adapter(entry.connection, entry.name)


def connection_refusal(entry: originals.StoreEntry) -> str | None:
    """Why a store's entry cannot name a connection of its own, or None.

    The media edge is one connection for the whole process (its address and credential come from the environment),
    so a declared further store of that adapter would silently be the store that serves eviction again: a replica in
    the same place, counted as a second failure domain.
    """
    adapter = entry.connection.get("adapter")
    if not entry.serves_eviction and adapter == "media-edge":
        return "a media-edge store has no connection of its own: only the store that serves eviction uses the edge"
    if adapter == B2_REPLICA_ADAPTER:
        if entry.serves_eviction:
            return "a B2 store can only hold replicas"
        if entry.head_recomputes is not False or entry.retrieval_class != originals.IMMEDIATE:
            return "a B2 replica store requires immediate retrieval and a metadata-only HEAD"
    return None


def head_reads_bytes(entry: originals.StoreEntry) -> bool:
    """Whether a scheduled HEAD can read ciphertext, even with a presence-only proof."""
    if entry.head_recomputes is True:
        return True
    try:
        return isinstance(_entry_adapter(entry), FilesystemStore)
    except MediaStoreError:
        return False


def connection_unusable(entry: originals.StoreEntry) -> str | None:
    """Why a store's connection cannot be built, or None. Building an adapter parses configuration and reads no store."""
    if (refusal := connection_refusal(entry)) is not None:
        return refusal
    try:
        _adapter(entry.connection, entry.name)
    except MediaStoreError as error:
        return str(error).removeprefix(f"{error.code}: ")
    return None


def declare_store(vault: Path, name: str) -> Path:
    """The owner's declaration that the directory in store `name`'s `root` is that store; it writes the root's marker.

    Only this writes a marker. It refuses a root on the vault's own device, because a NAS that is not mounted leaves
    its mount point there, and a marker written into that empty directory would defeat the marker's purpose. It
    never creates the root. It names the same store again without change and refuses a root that names another.
    """
    config = _settings(vault)
    entry = originals.store_entry(config, name)
    root = entry.connection.get("root") if entry is not None else None
    if entry is None or entry.connection.get("adapter") not in (FILESYSTEM_ADAPTER, FILESYSTEM_ADAPTER_ALIAS):
        raise MediaStoreError("MEDIA_STORE_DECLARATION_REFUSED", "that store is not a configured filesystem store")
    if not isinstance(root, str) or not Path(root).is_absolute() or not Path(root).is_dir():
        raise MediaStoreError("MEDIA_STORE_DECLARATION_REFUSED",
                              "the store's root must be an existing absolute directory: mount or create it first")
    folder = Path(root)
    try:
        with _hold_store_root(folder) as filesystem:
            _declare_held_store(vault, name, config, folder, filesystem)
    except (held_fs.HeldFsError, OSError) as error:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the store root cannot be verified") from error
    return folder


def _declare_held_store(
    vault: Path, name: str, config: dict[str, object], folder: Path, filesystem: held_fs.HeldFilesystem,
) -> None:
    from . import freshness, media_bindings, media_replicas, writer_lease

    _require_distinct_device(vault, filesystem)
    marker = _marker_in(filesystem)
    recorded = media_replicas.declared_store_id(vault, name)
    if marker is not None and marker["store"] != name:
        raise MediaStoreError("MEDIA_STORE_DECLARATION_REFUSED", "the store's root already names another store")
    if recorded is not None and (marker is None or marker.get("id") != recorded):
        raise MediaStoreError("MEDIA_STORE_DECLARATION_REFUSED",
                              "restore the original marker or choose a different store name")

    before = writer_lease.read_commit_generation(vault)
    checkpoint = media_bindings.source_checkpoint(vault)
    # A rebuilt ledger can forget the id. The durable sidecars still own the name.
    if before is None or (marker is None and media_bindings.store_is_bound(vault, name)):
        raise MediaStoreError("MEDIA_STORE_DECLARATION_REFUSED",
                              "originals still use this store name or its bindings cannot be verified")
    with _guard(vault, "media_store_declare"):
        if (writer_lease.read_commit_generation(vault) != before or freshness.external_pending(vault)
                or media_bindings.source_checkpoint(vault) != checkpoint or _settings(vault) != config
                or media_replicas.declared_store_id(vault, name) != recorded or _marker_in(filesystem) != marker):
            raise MediaStoreError(STORE_BUSY_CODE, "the vault or store changed while declaring it; retry")
        with filesystem.parent(".", access="flush").require() as parent:
            if marker is None:
                store_id = uuid.uuid4().hex
                data = _store_marker(name, store_id)
                held_fs.publish_bytes(filesystem, parent, STORE_MARKER, data).require()
                marker = _marker_in(filesystem)
                if marker is None or marker.get("id") != store_id:
                    raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the declared marker could not be confirmed")
            filesystem.flush_directory(parent).require()
        with held_fs._backend().acquire(folder, capability=filesystem.capabilities).require() as named:
            if (reserved_paths._identity_key(named.root_identity) != reserved_paths._identity_key(filesystem.root_identity)
                    or _marker_in(named) != marker):
                raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the named store changed during declaration")
        store_id = marker.get("id")
        if isinstance(store_id, str) and store_id != recorded:
            media_replicas.record_store_id(vault, name, store_id)


def _require_distinct_device(vault: Path, filesystem: held_fs.HeldFilesystem) -> None:
    with held_fs.acquire(vault).require() as vault_filesystem:
        if filesystem.root_identity.device == vault_filesystem.root_identity.device:
            raise MediaStoreError("MEDIA_STORE_DECLARATION_REFUSED",
                                  "the store's root is on the vault's own device, so it would not survive that device's loss")


@contextmanager
def _transport(operation: str):
    """A store transport fault is an outage, which leaves offload demand retryable."""
    try:
        yield
    except (held_fs.HeldFsError, OSError) as error:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", f"the media store {operation} failed: {error}") from error


def _reference_key(reference: StoredObject) -> str:
    return json.dumps(reference.fields(), sort_keys=True, separators=(",", ":"))


def _identity_text(device: int, inode: int, change_time_ns: int, mtime_ns: int, size: int) -> str:
    """The cache identity that a row records: the file's device, inode, change time, modification time and size."""
    return json.dumps((device, inode, change_time_ns, mtime_ns, size), separators=(",", ":"))


def _version_identity(version: held_fs.FileVersion) -> str:
    return _identity_text(version.identity.device, version.identity.inode, version.change_time_ns,
                          version.mtime_ns, version.size)


def _source_identity(source: reserved_paths.GenericBinarySource) -> str:
    return _version_identity(source.version)


def _identity(vault: Path, path: Path) -> str:
    try:
        with reserved_paths.open_generic_binary(vault, path.relative_to(vault).as_posix()) as source:
            return _source_identity(source)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code != "MISSING":
            raise
        snapshot = original_state(vault, path)
        if snapshot.local_path is None:
            raise
    from . import original_pool

    with original_pool.open_object(vault, snapshot.binary_sha256, snapshot.binary_size) as source:
        return _source_identity(source)


def _recorded_identity(vault: Path, path: Path, snapshot: OriginalSnapshot) -> str | None:
    """The verified physical identity for this digest; ordinary rows also retain their reference check.

    Reads without creating state or a store client.
    """
    if snapshot.state != "cached" or snapshot.reference is None:
        return None
    store = MediaJobStore(vault, create=False)
    if not store.path.exists():
        return None
    conn = store._connect(readonly=True)
    try:
        if conn.execute("SELECT 1 FROM sqlite_schema WHERE name = 'media_cache' AND type = 'table'").fetchone() is None:
            return None
        from . import media_bindings

        key = media_bindings.snapshot_key(vault, snapshot)
        row = conn.execute("SELECT * FROM media_cache WHERE binary_rel = ?", (key,)).fetchone()
        if (row is None or row["sha256"] != snapshot.binary_sha256
                or key == store._relative(path) and row["reference"] != _reference_key(snapshot.reference)):
            return None
        return row["identity"]
    finally:
        conn.close()


# SQLite's smallest default limit on bound parameters is 999; a chunk stays below it.
_LEDGER_PATHS_PER_QUERY = 500


def recorded_versions(
    vault: Path, wanted: list[tuple[originals.BinaryBinding, StoredObject]],
) -> list[bool]:
    """For each binding in `wanted`, whether the ledger records that exact version at its path.

    The media store writes that row itself when it offloads, reads or evicts the original there, and
    no note writes one, so a row is the store's own evidence that the version belongs at the path.
    Reads without creating state or a store client, a few paths per query; raises `sqlite3.Error`
    when the ledger cannot be read.
    """
    store = MediaJobStore(Path(vault).resolve(), create=False)
    if not wanted or not store.path.exists():
        return [False] * len(wanted)
    keys = [store._relative(store.vault_root / binary.path) for binary, _reference in wanted]
    paths = sorted(set(keys))
    rows: dict[str, tuple[str, str]] = {}
    with closing(store._connect(readonly=True)) as conn:
        if conn.execute("SELECT 1 FROM sqlite_schema WHERE name = 'media_cache' AND type = 'table'").fetchone() is None:
            return [False] * len(wanted)
        for start in range(0, len(paths), _LEDGER_PATHS_PER_QUERY):
            chunk = paths[start:start + _LEDGER_PATHS_PER_QUERY]
            rows.update(
                (row["binary_rel"], (row["reference"], row["sha256"]))
                for row in conn.execute(
                    f"SELECT binary_rel, reference, sha256 FROM media_cache WHERE binary_rel IN ({', '.join('?' * len(chunk))})",
                    chunk,
                )
            )
    return [rows.get(key) == (_reference_key(reference), binary.sha256)
            for key, (binary, reference) in zip(keys, wanted, strict=True)]


def cache_matches(vault: Path, path: Path, *, snapshot: OriginalSnapshot | None = None) -> bool:
    """Read a proven cache identity without creating state or a store client."""
    selected = snapshot if snapshot is not None else original_state(vault, path)
    recorded = _recorded_identity(vault, path, selected)
    return recorded is not None and recorded == _identity(vault, path)


def cached_sha256(vault: Path, path: Path, version: held_fs.FileVersion, *, strict_custody: bool = False) -> str | None:
    """The verified SHA-256 of a file at this exact version when the cache identity records it, else None.

    A caller that holds the file passes the version taken from its handle, so a match
    reads no byte (D3). None means the file has no record or no longer matches it.
    """
    vault = Path(vault).resolve()
    selected = _read_state(vault, path, strict_custody=True) if strict_custody else original_state(vault, path)
    try:
        recorded = _recorded_identity(vault, vault / path, selected)
    except sqlite3.Error:
        return None  # An unreadable record is no record, and the caller's hash then proves the bytes.
    return selected.binary_sha256 if recorded is not None and recorded == _version_identity(version) else None


def recover_cache(vault: Path, path: Path, snapshot: OriginalSnapshot) -> bool:
    if snapshot.state != "cached" or snapshot.reference is None:
        return False
    with _pin(vault, path) as (ledger, _token):
        # A pooled original has no file at its logical path; its shared object is the bytes to verify.
        with _open_local_original(vault, ledger._relative(path), snapshot) as source:
            if _digest(source.stream) != ObjectHead(snapshot.binary_size, snapshot.binary_sha256):
                return False
            source.require_current()
            with _guard(vault, "media_store_recover_cache"):
                if original_state(vault, path) != snapshot:
                    return False
                conn = ledger._connect()
                try:
                    with conn:
                        _record_cache(conn, ledger, snapshot, source)
                finally:
                    conn.close()
        return True


def _record_cache(conn, store: MediaJobStore, snapshot: OriginalSnapshot, source: reserved_paths.GenericBinarySource) -> None:
    assert snapshot.reference is not None
    source.require_current()
    from . import media_bindings

    key = media_bindings.snapshot_key(store.vault_root, snapshot)
    # `last_read` is the legacy record time; only `owner_read` decides idleness (D3, M4).
    conn.execute(
        "INSERT INTO media_cache(binary_rel, reference, sha256, identity, last_read, size, evicted) "
        "VALUES (?, ?, ?, ?, ?, ?, 0) "
        "ON CONFLICT(binary_rel) DO UPDATE SET reference=excluded.reference, sha256=excluded.sha256, "
        "identity=excluded.identity, last_read=excluded.last_read, size=excluded.size, evicted=0",
        (key, _reference_key(snapshot.reference), snapshot.binary_sha256,
         _source_identity(source), time.time(), snapshot.binary_size),
    )
    if snapshot.local_path is not None:
        media_bindings.coalesce_cache(conn, (key,))


def _clear_dead_access(conn, store: MediaJobStore, rows) -> int:
    """Delete the rows of dead processes, and remove the temporary file that each one names."""
    cleared = 0
    for row in rows:
        if holder_alive(row["pid"], row["pid_start"]):
            continue
        if row["source_custody_json"] is not None:
            # Native custody must prove this allocation; a reusable temporary name cannot authorize deletion.
            continue
        conn.execute("UPDATE media_access SET hydrating = 0 WHERE token = ?", (row["token"],))
        # Only a reserved publish name or a recorded pool stage is removed, so a damaged row cannot name an original.
        temporary = row["temporary_name"]
        if temporary is not None and not _remove_reserved_temporary(store.vault_root, row):
            continue  # The row keeps its bytes reserved until a later clear removes the file.
        conn.execute("DELETE FROM media_access WHERE token = ?", (row["token"],))
        cleared += 1
    return cleared


def _remove_reserved_temporary(vault: Path, row) -> bool:
    """Use the reservation's recorded custody, never a filename alone, for private stages."""
    from . import original_pool

    key, name = row["hydration_key"], row["temporary_name"]
    if key is not None and key != row["binary_rel"]:
        try:
            identity = None
            if row["temporary_identity"] is not None:
                values = json.loads(row["temporary_identity"])
                # StableIdentity's closed filesystem schema fixes these four fields.
                if (not isinstance(values, list) or len(values) != 4
                        or type(values[0]) is not int or type(values[1]) is not int
                        or values[2] != "file" or type(values[3]) is not int or values[3] != 1):
                    return False
                identity = held_fs.StableIdentity(*values)
            original_pool.remove_stage(vault, key, name, identity)
            return True
        except (ValueError, TypeError, OSError, held_fs.HeldFsError):
            return False
    return held_fs.is_publish_temporary(name) and _remove_temporary(vault, row["binary_rel"], name)


def _remove_temporary(vault: Path, binary_rel: str, name: str) -> bool:
    """Remove a dead hydration's temporary file; True once it is gone."""
    acquired = held_fs.acquire(vault)
    if not acquired.ok:
        return False
    with acquired.require() as filesystem:
        found = filesystem.parent(PurePosixPath(binary_rel).parent.as_posix(), access="mutate")
        if not found.ok:
            return found.error is not None and found.error.code == "MISSING"
        with found.require() as parent:
            residue = filesystem.file(parent, name, access="mutate")
            if not residue.ok:
                return residue.error is not None and residue.error.code == "MISSING"
            with residue.require() as file:
                return filesystem.unlink(file).ok


def clear_dead_reservations(vault: Path) -> int:
    """Clear every row of a dead process and remove its temporary file; runs at each runtime start."""
    if originals.binding_for(vault) is None:
        return 0
    store = MediaJobStore(Path(vault).resolve())
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cleared = _clear_dead_access(conn, store, conn.execute("SELECT * FROM media_access").fetchall())
        conn.commit()
        return cleared
    finally:
        conn.close()


@contextmanager
def _pin(vault: Path, path: Path, *, read: bool = False, object_key: str | None = None, strict_custody: bool = False):
    """Keep an original on the volume; `read` marks an owner's playback, download or viewing (D3, M4)."""
    from .cli_ops import OpError

    store = MediaJobStore(vault)
    relative = store._relative(path)
    token = uuid.uuid4().hex
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # A logical lookup can race canonical rebinding. NULL retains pooled
        # objects until acquisition binds this token to the actual held bytes.
        captured = object_key
        conn.execute("INSERT INTO media_access(token, binary_rel, pid, pid_start, object_key) VALUES (?, ?, ?, ?, ?)",
                     (token, relative, os.getpid(), process_start(os.getpid()), captured))
        conn.commit()
    finally:
        conn.close()
    try:
        yield store, token
    finally:
        custody_error = sys.exception()
        try:
            with closing(store._connect()) as conn, conn:
                if read:
                    conn.execute("UPDATE media_cache SET owner_read = MAX(COALESCE(owner_read, :read_at), :read_at) "
                                 "WHERE binary_rel = "
                                 "(SELECT object_key FROM media_access WHERE token = :token) "
                                 "AND identity = (SELECT object_identity FROM media_access WHERE token = :token) "
                                 "AND ((SELECT object_sha256 FROM media_access WHERE token = :token) IS NULL "
                                 "OR sha256 = (SELECT object_sha256 FROM media_access WHERE token = :token))",
                                 {"token": token, "read_at": time.time()})
                # A row that still names a temporary keeps it reserved until a clear removes the file.
                conn.execute("DELETE FROM media_access WHERE token = ? AND temporary_name IS NULL", (token,))
        except (sqlite3.Error, OpError) as cleanup_error:
            from .lexstore import classify_sqlite_error

            if not (isinstance(custody_error, reserved_paths.ReservedPathLeafError)
                    or strict_custody and custody_error is not None):
                raise
            if isinstance(cleanup_error, sqlite3.Error):
                busy = classify_sqlite_error(cleanup_error) == "transient"
            else:
                busy = cleanup_error.code == "MUTATION_BUSY" and cleanup_error.details.get("committed") is False
            if not busy:
                raise
            # The retained pin still protects the source. A later worker can clear it after this process stops.
            log.warning("pin cleanup deferred after a custody refusal")


def _bind_pin(store: MediaJobStore, token: str, snapshot: OriginalSnapshot,
              source: reserved_paths.GenericBinarySource, *, job: MediaJob | None = None) -> None:
    """Narrow an unresolved pin only while the caller holds the acquired original."""
    from . import media_bindings

    source.require_current()
    key = media_bindings.snapshot_key(store.vault_root, snapshot)
    if job is not None and not store.bind_original(job, key):
        # A stale claim or changed acquired object cannot process replacement bytes.
        raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the job no longer owns these original bytes")
    with closing(store._connect()) as conn, conn:
        conn.execute("UPDATE media_access SET object_key = ?, object_identity = ?, object_sha256 = ? "
                     "WHERE token = ? AND object_key IS NULL",
                     (key, _source_identity(source), snapshot.binary_sha256, token))


def _reserve(store: MediaJobStore, token: str, needed: int, limits: media_capacity.HydrationLimits, *,
             hydration: bool, temporary: str | None = None, hydration_key: str | None = None) -> None:
    """Reserve `needed` bytes of the hydration capacity on this pin's row (D12, choice 3).

    A hydration reserves its plaintext, takes a slot and names its temporary file. An
    offload reserves its ciphertext temporary file and takes no slot. Dead
    reservations and their files go first.
    """
    maximum, concurrent = limits.temporary_bytes, limits.hydrations
    if needed > maximum:
        raise MediaStoreError(HYDRATION_TOO_LARGE_CODE, "the original exceeds the whole hydration capacity")
    conn = store._connect()
    try:
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("BEGIN IMMEDIATE")
        _clear_dead_access(conn, store, conn.execute(
            "SELECT * FROM media_access WHERE temporary_bytes > 0 OR hydrating = 1 OR temporary_name IS NOT NULL").fetchall())
        row = conn.execute("SELECT COALESCE(SUM(temporary_bytes), 0), COALESCE(SUM(hydrating), 0) "
                           "FROM media_access WHERE temporary_bytes > 0 OR hydrating = 1").fetchone()
        same_object = hydration_key is not None and conn.execute(
            "SELECT 1 FROM media_access WHERE hydration_key = ? AND temporary_name IS NOT NULL AND token != ? LIMIT 1",
            (hydration_key, token)).fetchone() is not None
        busy = same_object or needed + row[0] > maximum or hydration and row[1] >= concurrent
        if not busy:
            # The destination coordinates downloads; only acquisition may establish object_key.
            conn.execute("UPDATE media_access SET temporary_bytes = ?, hydrating = ?, temporary_name = ?, "
                         "hydration_key = ?, temporary_identity = NULL WHERE token = ?",
                         (needed, int(hydration), temporary, hydration_key, token))
        conn.commit()  # The cleared rows stay cleared when the capacity is busy.
    finally:
        conn.close()
    if busy:
        raise MediaStoreError(STORE_BUSY_CODE, "hydration capacity is in use")


#: The two answers of ``managed_event``: look at the file again later, or leave its bytes alone.
DEFER = "defer"
SUPPRESS = "suppress"


def managed_event(vault: Path, path: Path) -> str | None:
    """Tell the watcher to defer an in-flight event or suppress proven cache bytes."""
    relative = Path(path).relative_to(vault).as_posix()
    if reserved_paths.classify_logical(relative).blocked:
        return None
    if originals.pooled(vault, path) is not None:
        # A pooled filing keeps no file at its logical path, with or without a media store: no file is no outside delete.
        return SUPPRESS
    if originals.binding_for(vault) is None:
        return None
    store = MediaJobStore(vault, create=False)
    if store.path.exists():
        conn = store._connect(readonly=True)
        try:
            if conn.execute("SELECT 1 FROM sqlite_schema WHERE name = 'media_access' AND type = 'table'").fetchone() is not None:
                rows = conn.execute("SELECT * FROM media_access WHERE binary_rel = ?", (store._relative(path),)).fetchall()
                # A read-only reader can see a ledger from before the start column, which the next write adds.
                if any(holder_alive(row["pid"], row["pid_start"] if "pid_start" in row.keys() else None) for row in rows):
                    return DEFER
        finally:
            conn.close()
    snapshot = original_state(vault, path)
    if snapshot.state == "evicted" or cache_matches(vault, path, snapshot=snapshot):
        return SUPPRESS
    return None


# A note or an original that is not a plain file (a symlink, a hard link or a reserved name) is not a binding to
# trust. These are the leaf boundary's structural refusal codes; any other one, such as a transient I/O refusal,
# reaches the caller, which must not miss a binding.
_UNTRUSTED_LEAF = frozenset({"UNSAFE_PATH", "RESERVED_PATH", "AMBIGUOUS_PATH"})


def bound_originals(
    vault: Path, *, root: Path | None = None, selects: Callable[[str], bool] | None = None,
    unconfirmed: Callable[[originals.RetirementUnconfirmed], None] | None = None,
) -> Iterator[OriginalSnapshot]:
    """Discover sidecar bindings, including unsupported or physically absent originals.

    `selects` takes a binding's vault-relative original path; a binding it rejects is never resolved.

    A binding whose Trash retirement is unconfirmed raises `originals.RetirementUnconfirmed`: its original is
    unknown, and a walk that left it out would read as complete. A sweep that may leave it out passes
    `unconfirmed`, which receives the error, and the walk goes on without that binding.
    """
    from .backfill import iter_kb_files
    from .vault import parse_frontmatter

    root = root if root is not None else managed_root(vault)
    if not root.is_dir():
        return
    from .vault_store import read as store_read

    archive = originals.ArchiveReads(vault)
    for sidecar in iter_kb_files(root):
        if sidecar.suffix.lower() != ".md":
            continue
        rel = sidecar.relative_to(vault).as_posix()
        try:
            raw = reserved_paths.read_generic_bytes(vault, rel)
            fields, _body, _marker = parse_frontmatter(raw.data.decode("utf-8"), custody=True)
            relative = originals.bound_path(fields)
            if not isinstance(relative, str) or (selects is not None and not selects(relative)):
                continue
            snapshot = original_state(vault, relative, archive=archive)
            if snapshot.sidecar_path == sidecar and snapshot.binary_sha256 is not None:
                yield snapshot
        except originals.RetirementUnconfirmed as error:
            # A page that names another companion's original, such as a trashed page that still names its old
            # path, binds nothing here; the walk meets that companion's own unknown binding where it lies.
            if error.companion != rel:
                continue
            if unconfirmed is None:
                raise
            unconfirmed(error)
        except (UnicodeError, ValueError):
            continue
        except reserved_paths.ReservedPathLeafError as error:
            if error.code in _UNTRUSTED_LEAF:
                continue
            # A store-mode file whose page the store moved or deleted is a projection the projector
            # has not settled yet: it binds nothing, and the page binds at its new path.
            if error.code == "MISSING" and store_read.serves(vault, rel):
                continue
            raise


def _status_candidates(vault: Path, table: str,
                       query: str | Callable[[sqlite3.Connection], tuple[str, tuple[object, ...]]],
                       parameters: tuple[object, ...] = ()) -> tuple[list[sqlite3.Row], dict[str, object]]:
    # These sources and states belong to this fixed status protocol; neither is vault vocabulary.
    coverage: dict[str, object] = {
        "source": table, "limit": STATUS_JOB_LIMIT, "coverage": "partial", "total": None,
        "state": "available",
    }
    ledger = MediaJobStore(vault, create=False)
    if not ledger.path.exists():
        return [], {**coverage, "state": "unavailable"}
    try:
        conn = ledger._connect(readonly=True)
        try:
            if conn.execute("SELECT 1 FROM sqlite_schema WHERE name = ? AND type = 'table'", (table,)).fetchone() is None:
                return [], {**coverage, "state": "unavailable"}
            if callable(query):
                query, parameters = query(conn)
            return conn.execute(query, (*parameters, STATUS_JOB_LIMIT)).fetchall(), coverage
        finally:
            conn.close()
    except (sqlite3.Error, OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
        return [], {**coverage, "state": "unavailable", "code": "MEDIA_STATUS_UNAVAILABLE"}


def _unavailable_original(path: str) -> dict[str, object]:
    # Candidate failures carry their path so terminal governance can withhold the entire row.
    return {"path": path, "state": "unavailable", "code": "MEDIA_ORIGINAL_UNVERIFIED"}


def _processing_status_row(vault: Path, path: str | Path) -> dict[str, object]:
    from . import media_processing
    from .vault import parse_frontmatter

    try:
        snapshot = original_state(vault, path)
    except (OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
        target = Path(path)
        return _unavailable_original(target.relative_to(vault).as_posix() if target.is_absolute() else target.as_posix())
    relative = snapshot.path.relative_to(vault).as_posix()
    try:
        raw = reserved_paths.read_generic_bytes(vault, snapshot.sidecar_path.relative_to(vault).as_posix())
        text = raw.data.decode("utf-8")
        fields, _body, _marker = parse_frontmatter(text, custody=True)
        processing = fields.get("processing_state")
        media_type = fields.get("media_type")
        if originals.bound_path(fields) != relative:
            return _unavailable_original(relative)
        completed = isinstance(media_type, str) and media_processing.has_completed_transcript(text, media_type=media_type)
        if processing == "completed" and not completed:
            processing = "unknown"
        elif processing is None:
            processing = "completed" if completed else "unknown"
        return {"path": relative, "sidecar_path": snapshot.sidecar_path.relative_to(vault).as_posix(),
                # 'stored' is the processing-status state fixed by the spec.
                "state": "stored" if snapshot.state == "evicted" else snapshot.state,
                "original_state": snapshot.state, "processing_state": processing}
    except (OSError, ValueError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
        return _unavailable_original(relative)


def _logical_cache_members(vault: Path, conn) -> tuple[str, tuple[object, ...]]:
    """Expand physical cache rows to filings, retaining each filing's own recovery reference."""
    from . import original_pool

    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'media_bindings'").fetchone():
        return "SELECT binary_rel, reference, sha256, size FROM media_cache ORDER BY binary_rel", ()
    prefix = original_pool.pool_root(vault).relative_to(vault).as_posix() + "/"
    return ("SELECT COALESCE(b.binary_rel, c.binary_rel) AS binary_rel, "
            "CASE WHEN b.binary_rel IS NULL THEN c.reference ELSE b.reference END AS reference, "
            "COALESCE(b.sha256, c.sha256) AS sha256, COALESCE(b.size, c.size) AS size "
            "FROM media_cache c LEFT JOIN media_bindings b ON b.object_key = c.binary_rel "
            "WHERE b.binary_rel IS NOT NULL OR substr(c.binary_rel, 1, length(?)) != ? "
            "ORDER BY c.binary_rel, b.binary_rel", (prefix, prefix))


def processing_status(vault: Path, *, path: str | None = None) -> tuple[list[dict[str, object]], dict[str, object]]:
    if path is not None:
        return [_processing_status_row(vault, path)], {"source": "sidecar", "coverage": "exact-path"}
    def query(conn):
        members, parameters = _logical_cache_members(vault, conn)
        return members + " LIMIT ?", parameters

    candidates, coverage = _status_candidates(vault, "media_cache", query)
    rows = [_processing_status_row(vault, row["binary_rel"]) for row in candidates]
    return [row for row in rows if row["state"] in {"stored", "unavailable"}], coverage


def enqueue_offload(vault: Path, path: Path) -> int:
    return _enqueue_offload(vault, path)


def _enqueue_offload(
    vault: Path, path: Path, *, usage: media_allowance.ReferenceUsage | None = None, census: bool = True,
) -> int:
    config = originals.configuration(vault)
    if config is None:
        return 0
    snapshot = original_state(vault, path)
    if snapshot.state != "local" or snapshot.binary_sha256 is None or not snapshot.offloadable:
        return 0
    store = MediaJobStore(vault)
    if _refused(store, snapshot, config, usage=usage, census=census) or _resident(vault, snapshot, config):
        return 0
    current = store.get_by_binary(snapshot.path, media_type=OFFLOAD_MEDIA_TYPE)
    if current is not None and current.do_offload:
        # An outage or a disabled store fails the attempt but keeps its demand;
        # an enabled store returns it to the queue. A disabled one keeps it parked.
        return int(config.get("enabled") is True and store.rearm_offload(current))
    # A separate row keeps offload runnable without changing extraction verdicts.
    store.enqueue(MediaJob(snapshot.path, snapshot.sidecar_path,
                           OFFLOAD_MEDIA_TYPE,
                           do_ocr=False, do_offload=True))
    return 1


def enqueue_offloads(vault: Path) -> int:
    from . import foreground_priority, media_bindings, original_pool
    from .writer_lease import get_manager

    if originals.binding_for(vault) is None:
        return 0
    if original_pool.enabled(vault):
        media_bindings.recover(vault)  # The census takes its own guard, so it runs before the scan's.
    with get_manager().consistency_guard(vault, operation="media_offload_scan", holder_kind="background"):
        placeholders = ",".join("?" for _code in media_allowance.ALLOWANCE_REFUSAL_CODES)
        rows, _coverage = _status_candidates(vault, "media_offload_refusals",
            f"SELECT binary_rel FROM media_offload_refusals WHERE code IN ({placeholders}) LIMIT ?",
            media_allowance.ALLOWANCE_REFUSAL_CODES)
        usage = media_allowance.reference_usage(vault) if rows else None
    # The walk reads every companion page, so it holds no boundary: one hold around it kept every save
    # waiting for minutes on a large vault. Each enqueue re-reads its original under a brief hold, and a
    # census older than the walk only decides what is queued: offload admission takes its own census.
    # With no census taken above, an allowance refusal recorded during the walk stays refused until the
    # next scan, instead of taking a whole-vault census inside an enqueue's hold.
    queued = 0
    with foreground_priority.bulk():  # the progress floor keeps a busy foreground from parking every item
        for path in foreground_priority.yielding(_offload_candidates(vault)):
            with get_manager().consistency_guard(vault, operation="media_offload_scan", holder_kind="background"):
                queued += _enqueue_offload(vault, path, usage=usage, census=usage is not None)
    return queued


def _offload_candidates(vault: Path) -> Iterator[Path]:
    """Each bound original that the offload scan checks.

    A shared archive resolves only the bindings of its current projection that hold no recovery reference, so a
    scan of an offloaded archive reads no companion. Any other archive, and a projection that its census could not
    make current, walks every companion. A projected binding resolves as the walk resolves the companion it meets.
    """
    from . import media_bindings, original_pool
    from .vault import VAULT_SCAN_SKIP_DIRS
    from .vault_store import read as store_read

    root = Path(vault).resolve()
    rows = media_bindings.unreferenced(root) if original_pool.enabled(root) else None
    if rows is None:
        yield from (item.path for item in bound_originals(vault, unconfirmed=skip_unknown_original))
        return
    managed = managed_root(root)
    for binary_rel, companion_rel in rows:
        binary = root / binary_rel
        # The walk meets only a companion beside its original, under the managed root, below no folder it prunes.
        if (companion_rel not in originals.companion_paths(binary_rel) or not binary.is_relative_to(managed)
                or any(part in VAULT_SCAN_SKIP_DIRS for part in binary.parent.relative_to(managed).parts)):
            continue
        try:
            snapshot = _bound_file(root, binary_rel)
        except originals.RetirementUnconfirmed as error:
            skip_unknown_original(error)
            continue
        except reserved_paths.ReservedPathLeafError as error:
            # A companion that the store moved or deleted binds at its new path.
            if error.code == "MISSING" and store_read.serves(root, companion_rel):
                continue
            raise
        if snapshot is not None:
            yield snapshot.path


def skip_unknown_original(error: originals.RetirementUnconfirmed) -> None:
    """Log a background sweep's skipped binding as unknown; the sweep claims nothing about it."""
    log.warning("skipped the original that %s names: its state is unknown (%s)", error.companion, error.code)


def _guard(vault: Path, operation: str):
    from .writer_lease import get_manager

    return get_manager().mutation_guard(vault, operation=operation, holder_kind="background")


def _require_current_local_policy(vault: Path, config: dict[str, object], backend: Store) -> None:
    if not _personal_install():
        return
    if originals.configuration(vault) != config:
        raise MediaStoreError(STORE_BUSY_CODE, "the media store policy changed before publication")
    if isinstance(backend, FilesystemStore):
        from . import media_replicas

        if media_replicas.declared_store_id(vault, backend.name) != backend.store_id:
            raise MediaStoreError(STORE_BUSY_CODE, "the filesystem store declaration changed before publication")


def _personal_install() -> bool:
    from .cloud_cell import store_identity

    return store_identity()["kind"] == "personal"


def _personal_filesystem(backend: Store) -> bool:
    return isinstance(backend, FilesystemStore) and _personal_install()


def _publishing(vault: Path, operation: str, unit: Callable[[], None], *,
                publishing: Sequence[StoredObject] = (), copied_from: StoredObject | None = None) -> None:
    """Run a unit that publishes a reference (``_publish_reference``) under the vault boundary.

    Its sidecar commit runs on the store thread in a store-mode vault, so the unit runs there
    (``vault_store.media.holding_boundary``). A boundary that stays busy is ordinary contention,
    reported as ``STORE_BUSY_CODE``, which returns an offload job to the queue without spending an
    attempt.

    Check the versions the unit will list and the version it copied. A finding stops publication with nothing written,
    so the request waits for a new choice. A plaintext finding that lands after the check reaches the new version
    through its row write (`media_replicas.record`).
    """
    from . import foreground_priority, media_replicas
    from .vault_store import media as store_media
    from .vault_store import projector

    versions = (*publishing, copied_from) if copied_from is not None else publishing
    stop = foreground_priority.cancellation_event()

    def checked() -> None:
        if stop is not None and stop.is_set():
            raise MediaStoreError(STORE_BUSY_CODE, "the background publication was stopped")
        if versions and media_replicas.has_quarantined(vault, versions):
            raise MediaStoreError(STORE_BUSY_CODE, "a version needed for publication was found damaged")
        unit()

    try:
        store_media.holding_boundary(vault, operation, checked, lambda: _guard(vault, operation))
    except projector.BoundaryBusy as busy:
        raise MediaStoreError(STORE_BUSY_CODE, "the vault boundary is busy") from busy


def _binding_of(vault: Path, snapshot: OriginalSnapshot) -> originals.BinaryBinding:
    assert snapshot.binary_sha256 is not None and snapshot.binary_size is not None
    return originals.BinaryBinding(snapshot.path.relative_to(vault).as_posix(),
                                   snapshot.binary_sha256, snapshot.binary_size)


class OffloadBatch:
    """One owner census for a run of offloads, kept across guard releases (D10).

    The batch never holds the mutation boundary, so other writes run between two members. Both offload
    and repair note any new pointer before publishing under that boundary. `charge` adds the pointer to
    the publishing batch. Any other batch reads the census again before it can publish.
    `end` drops the census so that the next member reads it again, and `failed` does so only for a
    member that reached `charge`: a refused member bound nothing, so the next one reuses the census.
    A stale census can wrongly refuse a member, which the next scan releases, because refusals are
    read again from a fresh census.
    """

    def __init__(self, vault: Path, *, record_status: bool = True) -> None:
        self._vault = Path(vault).resolve()
        self._record_status = record_status
        self._usage: media_allowance.ReferenceUsage | None = None
        self._charged = False
        self._replacements: str | None = None

    def usage(self, ledger: MediaJobStore) -> media_allowance.ReferenceUsage:
        try:
            replacements = _replacements_noted(ledger)
        except _LEDGER_UNREADABLE:
            replacements = None  # a marker that cannot be read may have changed: read the census again, as for any other change
        if replacements is None or replacements != self._replacements:
            self._usage = None
        if self._usage is None:
            # Read before the census, so that a replacement that lands during the walk is seen by the next call.
            self._replacements = replacements
            self._usage = media_allowance.reference_usage(self._vault)
            if self._record_status:
                _record_census(ledger, self._usage)
        return self._usage

    def begin(self) -> None:
        self._charged = False

    def charge(self, ledger: MediaJobStore, snapshot: OriginalSnapshot, reference: StoredObject,
               usage: media_allowance.ReferenceUsage) -> None:
        self._charged = True
        usage.charge(_binding_of(self._vault, snapshot), reference)
        self._replacements = _note_replacement(ledger)
        if self._record_status:
            _record_census(ledger, usage)

    def failed(self) -> None:
        """A member raised: drop the census when that member may have half-published, which the census cannot know."""
        if self._charged:
            self.end()

    def end(self) -> None:
        self._usage = None


#: The `meta` key whose value changes before a publication binds a pointer.
_REPLACEMENTS = "pointer_replacements"


def _note_replacement(ledger: MediaJobStore) -> str:
    """Invalidate other active censuses before publishing a pointer."""
    marker = uuid.uuid4().hex
    _durable(ledger, "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
             (_REPLACEMENTS, marker))
    return marker


def _replacements_noted(ledger: MediaJobStore) -> str:
    """The marker as last written, or an empty string when no copy again has written one."""
    with closing(ledger._connect(readonly=True)) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (_REPLACEMENTS,)).fetchone()
    return "" if row is None else str(row[0])


# What reading or writing the media ledger can raise.
_LEDGER_UNREADABLE = (sqlite3.Error, OSError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError)


def _record_census(ledger: MediaJobStore, usage: media_allowance.ReferenceUsage) -> None:
    """Keep the latest owner census for the window status, so that a status call never walks the vault (D12).

    A census that could not count is kept as unknown, so that the status never shows an older figure in its
    place. A failed write is dropped, because it costs the status only a fresher figure and must not stop an offload.
    """
    with suppress(*_LEDGER_UNREADABLE), closing(ledger._connect()) as conn, conn:
        conn.execute(
            "INSERT INTO media_owner_census(singleton, used_bytes, observed_at, blockers) "
            "VALUES (1, ?, ?, ?) ON CONFLICT(singleton) DO UPDATE SET used_bytes=excluded.used_bytes, "
            "observed_at=excluded.observed_at, blockers=excluded.blockers",
            (usage.used_bytes, usage.observed_at, json.dumps([[item.path, item.reason] for item in usage.blockers])),
        )


@dataclass(frozen=True, slots=True)
class RecordedCensus:
    """An owner census as recorded: the bytes in use, or None when it could not count, and when it was read."""

    used_bytes: int | None
    observed_at: str
    blockers: tuple[media_allowance.Blocker, ...] = ()

    def older_than(self, seconds: float) -> bool:
        """Whether the census is older than `seconds`; a time that cannot be read counts as older."""
        try:
            observed = datetime.fromisoformat(self.observed_at)
        except ValueError:
            return True
        return observed.tzinfo is None or (datetime.now(UTC) - observed).total_seconds() > seconds


def take_census(vault: Path) -> RecordedCensus:
    """Count the owner references now and record the result, as an offload batch does; for a status with a demand (D12)."""
    vault = Path(vault).resolve()
    usage = media_allowance.reference_usage(vault)
    with suppress(*_LEDGER_UNREADABLE):  # The reading is still returned; only the record is lost.
        _record_census(MediaJobStore(vault), usage)
    return RecordedCensus(usage.used_bytes, usage.observed_at, usage.blockers)


def recorded_census(vault: Path) -> RecordedCensus | None:
    """The latest census that offload recorded, or None when it recorded none.

    Raises `MediaStoreError` when the ledger cannot be read. A ledger from before the record has no table: none.
    """
    ledger = MediaJobStore(Path(vault).resolve(), create=False)
    if not ledger.path.exists():
        return None
    try:
        with closing(ledger._connect(readonly=True)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'media_owner_census'").fetchone() is None:
                return None
            columns = {str(column["name"]) for column in conn.execute("PRAGMA table_info(media_owner_census)")}
            row = conn.execute("SELECT * FROM media_owner_census").fetchone()
    except _LEDGER_UNREADABLE as error:
        raise MediaStoreError("MEDIA_LEDGER_UNREADABLE", f"the media ledger cannot be read: {error}") from error
    if row is None:
        return None
    # A ledger from before the blockers were recorded has no such column: an unknown census names no note.
    named = json.loads(row["blockers"] or "[]") if "blockers" in columns else []
    return RecordedCensus(
        None if row["used_bytes"] is None else int(row["used_bytes"]), str(row["observed_at"]),
        tuple(media_allowance.Blocker(path, reason) for path, reason in named))


# Bound how many live bindings and store HEADs one offload spends on equal versions.
_REUSE_CANDIDATE_LIMIT = 8


def _reuse_candidates(
    vault: Path, binary: originals.BinaryBinding, usage: media_allowance.ReferenceUsage, key_version: str,
) -> tuple[list[tuple[Path, StoredObject]], list[StoredObject]]:
    """One indexed slice of matching versions and their recorded damage."""
    from . import media_replicas

    found = usage.reuse_candidates(binary, key_version, limit=_REUSE_CANDIDATE_LIMIT)
    damaged = media_replicas.damaged_among(vault, [candidate for _path, candidate in found])
    ready: list[tuple[Path, StoredObject]] = []
    failed: list[StoredObject] = []
    for path, reference in found:
        if (reference.store, reference.object, reference.version) in damaged:
            usage.skip_reuse(binary, reference)
            failed.append(reference)
        else:
            ready.append((path, reference))
    return ready, failed


def _live_filing(vault: Path, binary: originals.BinaryBinding, path: Path, reference: StoredObject) -> OriginalSnapshot:
    """The filing at `path` as it is now, when it still lists `reference` for these bytes; else `ValueError`."""
    candidate = original_state(vault, path)
    if (candidate.reference != reference or candidate.binary_sha256 != binary.sha256
            or candidate.binary_size != binary.size):
        raise ValueError("the live reuse binding changed")
    return candidate


#: What reading a filing can raise when it is gone or changed: the census can no longer vouch for it.
_FILING_UNREADABLE = (OSError, ValueError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError)


def _reuse_walk_inputs(
    vault: Path, snapshot: OriginalSnapshot, census: Callable[[], media_allowance.ReferenceUsage], key_version: str,
) -> tuple[media_allowance.ReferenceUsage, list[tuple[Path, StoredObject]], list[StoredObject]]:
    """The census and the versions of equal bytes that offload may reuse, read once under one read boundary."""
    from .writer_lease import get_manager

    binary = _binding_of(vault, snapshot)
    with get_manager().consistency_guard(vault, operation="media_offload_admission", holder_kind="background"):
        usage = census()
        candidates, damaged = _reuse_candidates(vault, binary, usage, key_version)
    return usage, candidates, damaged


def _offload_admission(
    vault: Path, snapshot: OriginalSnapshot, *, reference: StoredObject | None = None,
    select_reuse: bool = True, usage: media_allowance.ReferenceUsage | None = None,
) -> tuple[media_allowance.AllowanceDecision, OriginalSnapshot | None]:
    """Select current live reuse and its quota projection under one read boundary.

    The reuse is the first live filing of a version of equal bytes that no finding recorded as damaged
    (`_reuse_candidates`). Offload confirms its candidates against the store itself (`_offload_member`).
    """
    from .writer_lease import get_manager

    binary = _binding_of(vault, snapshot)
    with get_manager().consistency_guard(vault, operation="media_offload_admission", holder_kind="background"):
        config = originals.configuration(vault) or {}
        # A caller shares one census while its enclosing consistency guard holds, as an `OffloadBatch`, or
        # across the offload scan's per-item holds, where admission re-checks with its own census.
        if usage is None:
            usage = media_allowance.reference_usage(vault)
        reuse = None
        if select_reuse:
            for path, candidate_reference in _reuse_candidates(vault, binary, usage, str(config.get("key_version")))[0]:
                try:
                    reuse, reference = _live_filing(vault, binary, path, candidate_reference), candidate_reference
                except _FILING_UNREADABLE:
                    usage = media_allowance.ReferenceUsage(None, usage.observed_at)
                break
        return media_allowance.admission(usage, binary, reference, config.get("allowance_bytes")), reuse


def upload_refusal(
    vault: Path, declared_bytes: int, filename: str | None, lane: str, *, most_bytes: int | None = None,
) -> str | None:
    """Why `MEDIA_ALLOWANCE_EXCEEDED` refuses an upload of `declared_bytes`, or None when it is admitted (D10, task 4.5).

    Only an upload that offload would store charges the allowance. Preserve keeps an Evidence note or dataset
    on the volume (design D1), as does a pooled archive for a suffix-less name (`originals.resident_suffixless`).
    The Source lane files every other upload through `add` as an offloadable original, whatever its name, so
    a Source upload whose name is not known yet is charged. An Evidence upload whose name is not known yet
    (`filename` None) is admitted. A size known only as a range, at least `declared_bytes` and at most
    `most_bytes`, is refused only when every size in it is. An artifact too large to offload stays on the volume in either lane. The recorded
    census decides first. Only a record that says the upload does not fit is confirmed by a census read now,
    and only the confirmed figure refuses. No record, an unknown figure, or a configuration, volume or ledger
    that cannot be read admits the upload: its original stays local with retryable offload demand, and offload
    rechecks the allowance before publication. The declared size counts as a new version, because nothing
    shows a duplicate of a stored version before the bytes arrive.
    """
    from . import held_uploads, source_inspection

    vault = Path(vault).resolve()
    try:
        config = originals.configuration(vault)
        if config is None or config.get("enabled") is not True:
            return None
        bounds = media_capacity.bounds_for(config, vault)
        # An import archive is unpacked into a stage and released, so it is never an offloaded original. `evidence` is
        # the upload lane token (upload_tokens.UPLOAD_LANES) whose filing keeps notes and datasets.
        stays = lane == held_uploads.IMPORT_LANE or (lane == "evidence" and (
            filename is None or originals.is_note_path(filename) or source_inspection.is_dataset_name(filename))) or (
            filename is not None and originals.resident_suffixless(vault, filename))
        largest = declared_bytes if most_bytes is None else most_bytes
        # A larger size never classifies back to offloadable, so the largest possible size decides for the range.
        if stays or (bounds is not None and bounds.classify(largest) != media_capacity.OFFLOADABLE):
            return None
        # The record, not a census read now, keeps a whole-vault walk under the consistency guard off each
        # upload: the per-item census cost that task 5.4.9 removed from offload.
        recorded = recorded_census(vault)
        if recorded is None:
            return None
        decision = _upload_decision(recorded, declared_bytes, config.get("allowance_bytes"))
        if decision.code == media_allowance.MEDIA_ALLOWANCE_EXCEEDED:
            decision = _upload_decision(take_census(vault), declared_bytes, config.get("allowance_bytes"))
    except (OSError, ValueError, TypeError, MediaStoreError, held_fs.HeldFsError) as error:  # An unreadable policy, volume, ledger or pool marker.
        log.warning("the upload allowance check admits an upload as unknown: %s", type(error).__name__)
        return None
    if decision.code != media_allowance.MEDIA_ALLOWANCE_EXCEEDED:
        return None
    # The owner's used total stays out: D10 withholds global usage from restricted principals.
    size = f"{declared_bytes:,}" if most_bytes is None else f"{declared_bytes:,} or more"
    return (f"the upload's {size} bytes exceed what remains of the media allowance of "
            f"{decision.allowance_bytes:,} bytes; delete stored media and empty Trash, or raise the allowance")


def _upload_decision(
    census: RecordedCensus, declared_bytes: int, allowance_bytes: object,
) -> media_allowance.AllowanceDecision:
    projected = None if census.used_bytes is None else census.used_bytes + declared_bytes
    return media_allowance.decide(census.used_bytes, projected, allowance_bytes, census.observed_at)


@dataclass(frozen=True, slots=True)
class ReferencePublication:
    """The exact guarded companion image prepared before a placement transition."""

    snapshot: OriginalSnapshot
    write: PlannedWrite
    retirement_meta: str | None = None


def prepare_reference(vault: Path, snapshot: OriginalSnapshot, reference: StoredObject | None,
                      *, retirement_meta: str | None = None) -> ReferencePublication:
    """Prepare the existing reference edit once, retaining its source guard and hash."""
    def edit(fields: dict[str, object]) -> None:
        if reference is None:
            fields.pop("stored_object", None)
        else:
            fields["stored_object"] = reference.fields()
        if retirement_meta is not None:
            fields["original_retirement_meta"] = retirement_meta

    return _prepare_edit(vault, snapshot, edit, retirement_meta)


def prepare_replica(vault: Path, snapshot: OriginalSnapshot, replica: StoredObject) -> ReferencePublication:
    """Prepare the edit that lists `replica` beside the sidecar's other replicas; `stored_object` stays as written."""
    def edit(fields: dict[str, object]) -> None:
        fields[originals.REPLICAS] = originals.with_replica(fields.get(originals.REPLICAS), replica)

    return _prepare_edit(vault, snapshot, edit, None)


def _prepare_edit(vault: Path, snapshot: OriginalSnapshot, edit: Callable[[dict[str, object]], None],
                  retirement_meta: str | None) -> ReferencePublication:
    from .vault import (
        PlannedWrite,
        content_hash,
        parse_frontmatter,
        read_guarded_text,
        render_frontmatter_document,
        serialize_frontmatter,
    )
    text, guard = read_guarded_text(vault, snapshot.sidecar_path)
    if content_hash(text) != snapshot.sidecar_hash:
        raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the sidecar binding changed")
    fields, body, _marker = parse_frontmatter(text, custody=True)
    edit(fields)
    rendered = render_frontmatter_document(serialize_frontmatter(fields), body)
    return ReferencePublication(snapshot, PlannedWrite(
        snapshot.sidecar_path, rendered, guard=guard, expected_hash=content_hash(text),
    ), retirement_meta)


def _commit_reference(vault: Path, publication: ReferencePublication) -> None:
    from . import preserve
    from .vault_store import read

    snapshot, write = publication.snapshot, publication.write
    rendered = write.content.encode("utf-8")
    with trash_metadata.completing_retirement(publication):
        preserve.commit_media_sidecar_writes(vault, (write,))
    # Keep protection through an uncertain writer or catalog result. Explicitly
    # confirm durable bytes even when a prior attempt already wrote this value.
    relative = snapshot.sidecar_path.relative_to(vault).as_posix()
    # A store-mode vault's page is the store's, committed durably above, and its Markdown file may
    # not be projected yet: the read-back is the store's page, with no file to sync.
    if (stored := read.owned_bytes(vault, relative)) is not None:
        if stored != rendered:
            raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "reference publication is uncertain")
        return
    with reserved_paths.open_generic_binary(vault, relative) as source:
        if source.stream.read() != rendered:
            raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "reference publication is uncertain")
        try:
            os.fsync(source.stream.fileno())
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
            # Windows flushes a handle opened for writing only, and this stream is read-only.
            _flush_through_writer(vault, relative, source.version.identity)
    held_fs.flush_directory_path(snapshot.sidecar_path.parent).require()


def _flush_through_writer(vault: Path, relative: str, expected: held_fs.StableIdentity) -> None:
    """Flush the file at ``relative`` through a write handle, once it is the file that was read."""
    with held_fs.acquire(vault).require() as filesystem:
        with filesystem.parent(PurePosixPath(relative).parent.as_posix()).require() as parent:
            with filesystem.file(parent, PurePosixPath(relative).name, access="write").require() as writable:
                if (writable.identity.device, writable.identity.inode) != (expected.device, expected.inode):
                    raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "reference publication is uncertain")
                os.fsync(writable.descriptor)


def _publish_reference(vault: Path, snapshot: OriginalSnapshot, reference: StoredObject | None) -> None:
    _commit_reference(vault, prepare_reference(vault, snapshot, reference))


def _verified_head(store: Store, reference: StoredObject, *, deadline: float | None = None,
                   scheduled: ScheduledRead | None = None) -> ObjectHead | None:
    """The version's HEAD, None when it is definitively absent. A different ciphertext raises."""
    from .media_b2_store import B2ReplicaStore
    from .media_edge_store import EdgeStore

    with _transport("head"):
        if isinstance(store, FilesystemStore):
            head = store.head(reference.object, reference.version, expected_size=reference.ciphertext_size,
                              deadline=deadline, scheduled=scheduled)
        elif deadline is not None and isinstance(store, (EdgeStore, B2ReplicaStore)):
            head = store.head(reference.object, reference.version, deadline=deadline)
        else:
            head = store.head(reference.object, reference.version)
    if head is not None and head.size != reference.ciphertext_size:
        raise ReplicaMismatch("size_mismatch")
    if head is not None and head.sha256 != reference.ciphertext_sha256:
        raise ReplicaMismatch("digest_mismatch")
    return head


#: The binding states whose bytes are on this volume (`originals` fixes them).
_BYTES_HERE = ("local", "cached")
#: The failures of `_head_failure` that say the version is not there to read, as opposed to its bytes differing.
_ABSENT, _CONDEMNED = ReplicaAbsent.failure, "condemned"


def _head_failure(backend: Store, replica: StoredObject, *, deadline: float | None = None,
                  scheduled: ScheduledRead | None = None) -> str | None:
    """Ask `backend` about the replica's version: None when it matches the entry, else why it does not.

    The answers are `absent`, `condemned`, `size_mismatch` and `digest_mismatch`. A store that cannot answer raises
    `MEDIA_STORE_UNAVAILABLE`: that is no finding, and the caller keeps what it had.
    """
    try:
        head = _verified_head(backend, replica, deadline=deadline, scheduled=scheduled)
    except ReplicaMismatch as mismatch:
        return mismatch.failure
    if head is None:
        return _ABSENT
    # A condemned version still reads until the bucket rule deletes it, and nothing may rely on it (collect-released-media D5).
    return _CONDEMNED if head.condemned else None


def check_replica(vault: Path, replica: StoredObject, *, deadline: float | None = None,
                  scheduled: ScheduledRead | None = None) -> str | None:
    """Check the stored version, retaining a partial filesystem hash at a round boundary."""
    pending = False
    try:
        failure = _head_failure(store_for(vault, replica.store), replica, deadline=deadline, scheduled=scheduled)
    except ReplicaCheckPending:
        pending = True
        raise
    finally:
        if scheduled is not None and not pending:
            scheduled.reset()
    if failure == _ABSENT:
        raise ReplicaAbsent()
    return failure


class _NullSink:
    """A sink that drops what it is given: the decrypt pass already hashes the plaintext, and a sample keeps none of it."""

    def write(self, chunk: bytes) -> int:
        return len(chunk)

    def flush(self) -> None:
        return None


class StoreReadPending(MediaStoreError):
    """A preceding operation still owns this store's maintenance read slot."""

    def __init__(self) -> None:
        super().__init__(STORE_BUSY_CODE, "a preceding replica operation is still releasing its read")


class StoreReadTimedOut(MediaStoreError):
    """The maintenance worker ended its wait for a store operation."""

    def __init__(self) -> None:
        super().__init__("MEDIA_STORE_UNAVAILABLE", "the replica store operation exceeded its deadline")


class CopyStoreUnavailable(MediaStoreError):
    """The destination could not be opened or its identity could not be confirmed before a copy."""

    def __init__(self, message: str = "the copy destination did not answer as itself") -> None:
        super().__init__("MEDIA_STORE_UNAVAILABLE", message)


_ReadResult = TypeVar("_ReadResult")
_pending_reads: set[tuple[Path, str]] = set()
_pending_reads_lock = threading.Lock()
# A marker probe should leave most of the maintenance turn for the actual transfer.
STORE_PROBE_SECONDS = 2.0


def wait_store_read(vault: Path, name: str, read: Callable[[], _ReadResult], *, timeout: float) -> _ReadResult:
    """Wait for read-only adapter I/O. An unanswered syscall keeps only its own store's slot."""
    if timeout <= 0:
        raise StoreReadPending()
    key = vault, name
    with _pending_reads_lock:
        if key in _pending_reads:
            raise StoreReadPending()
        _pending_reads.add(key)
    completed: Queue[tuple[float, _ReadResult | None, BaseException | None]] = Queue(maxsize=1)

    def release() -> None:
        with _pending_reads_lock:
            _pending_reads.discard(key)

    def read_and_release() -> None:
        try:
            value = read()
            outcome = (time.monotonic(), value, None)
        except BaseException as error:  # noqa: BLE001 - report on the calling thread
            outcome = (time.monotonic(), None, error)
        finally:
            release()
        completed.put_nowait(outcome)

    deadline = time.monotonic() + timeout
    try:
        threading.Thread(target=read_and_release, name="media-replica-read", daemon=True).start()
    except BaseException:
        release()
        raise
    try:
        finished, value, failure = completed.get(timeout=max(0.0, deadline - time.monotonic()))
    except Empty as error:
        raise StoreReadTimedOut() from error
    if finished >= deadline:
        raise StoreReadTimedOut()
    if failure is not None:
        raise failure
    return cast(_ReadResult, value)


def sample_replica(vault: Path, replica: StoredObject, expected: ObjectHead, *, deadline: float | None = None) -> str | None:
    """Download and decrypt `replica` and hash its plaintext against `expected`: None when it matches, else why not.

    The answers are `size_mismatch` and `digest_mismatch` (the ciphertext differs from its record) and
    `plaintext_mismatch` (it decrypts to other bytes than the binding). The identity refusing ciphertext that matches
    its record raises `KeyRefused`, which is no damage; an unavailable store, or an age that was stopped, raises
    `MEDIA_STORE_UNAVAILABLE`.
    """
    from . import media_replicas

    def read() -> str | None:
        try:
            head = write_stored_original(vault, replica, _NullSink(), store=replica.store, prove=True, deadline=deadline)
        except ReplicaMismatch as mismatch:
            return mismatch.failure
        return None if head == expected else media_replicas.PLAINTEXT_MISMATCH

    if deadline is None:
        return read()
    finding = wait_store_read(vault, replica.store, read, timeout=deadline - time.monotonic())
    _require_transfer_time(deadline)
    return finding


def replica_damage(error: MediaStoreError) -> str | None:
    """The finding from a confirmed absence or a ciphertext mismatch, if the read proved one."""
    return error.failure if isinstance(error, (ReplicaAbsent, ReplicaMismatch)) else None


def _quarantine_found(vault: Path, binary_rels: Sequence[str], reference: StoredObject, failure: str, *,
                      entry: originals.StoreEntry | None = None) -> None:
    """Record that the store does not hold `reference` as named.

    The level is the one the store's descriptor lets a check prove, so a store that cannot recompute never reads as
    content-checked.
    """
    from . import media_replicas

    if entry is None:
        entry = originals.store_entry(_settings(vault), reference.store)
    level = media_replicas.scheduled_level(entry) if entry is not None else None
    media_replicas.quarantine_all(vault, binary_rels, reference, level=level or media_replicas.PRESENCE, failure=failure)


def _confirmed(store: Store, reference: StoredObject) -> bool:
    """Whether the version may stand for the bytes: it exists, matches and is current.

    A condemned version never does (collect-released-media D5): nothing evicts on it, reuses it or publishes it.
    """
    head = _verified_head(store, reference)
    return head is not None and not head.condemned


def forget_original(vault: Path, path: str | Path, *, prepared: ReferencePublication | None = None) -> None:
    """Remove a deleted original's binding before its explicit deletion fan-out."""
    if originals.binding_for(vault) is None:
        return
    snapshot = original_state(vault, path)
    if snapshot.reference is None:
        return
    reference = snapshot.reference

    def forget() -> None:
        if original_state(vault, path) != snapshot:
            raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the deletion binding changed")
        _release_name(MediaJobStore(vault), reference.object)
        if prepared is None:
            _publish_reference(vault, snapshot, None)
        else:
            acquired = prepared.snapshot
            if (acquired.path, acquired.sidecar_path, acquired.sidecar_hash, acquired.reference) != (
                    snapshot.path, snapshot.sidecar_path, snapshot.sidecar_hash, snapshot.reference):
                raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the prepared deletion binding changed")
            _commit_reference(vault, prepared)

    _publishing(vault, "media_store_forget_original", forget)


def _durable(store: MediaJobStore, statement: str, parameters: tuple[object, ...]) -> None:
    """One statement, committed with a full sync: a crash-consistent clone taken after it holds the row."""
    conn = store._connect()
    try:
        # D9 requires FULL before BEGIN: NORMAL can omit this commit from a clone.
        conn.execute("PRAGMA synchronous=FULL")
        with conn:
            conn.execute(statement, parameters)
    finally:
        conn.close()


def _release_name(store: MediaJobStore, name: str) -> None:
    """collect-released-media D5: record that a reference to `name` is about to retire.

    The caller commits this before it removes the reference, so a crash between the two keeps the reference. A
    released name is only a candidate: a hold condemns it once no retained backup references or protects it.
    """
    _durable(store, "INSERT INTO media_releases(object) VALUES (?)", (name,))


def _upload_name(store: MediaJobStore, snapshot: OriginalSnapshot, replica_store: str = "") -> str:
    """collect-released-media D1: the name minted for this original's open record, or a fresh random one.

    A retry after a lost reply reuses the record's name, which keeps the edge's nonce recovery and serial-write
    hold. A name that a sidecar may carry is sealed (`_seal`) and never receives another upload. The record is per
    store: `replica_store` is the replica store the name is for, and empty is the store that serves eviction.
    """
    conn = store._connect(readonly=True)
    try:
        row = conn.execute("SELECT object FROM media_protections WHERE binary_rel = ? AND uploadable = 1 "
                           "AND store = ? ORDER BY created_at LIMIT 1",
                           (store._relative(snapshot.path), replica_store)).fetchone()
    finally:
        conn.close()
    return str(row["object"]) if row is not None else secrets.token_hex(32)


def _protect(store: MediaJobStore, snapshot: OriginalSnapshot, name: str, *, uploadable: bool,
             replica_store: str = "", conn: sqlite3.Connection | None = None) -> None:
    # One record per object covers every retried attempt for this original.
    relative = store._relative(snapshot.path)
    statement = ("INSERT INTO media_protections(token, binary_rel, object, created_at, uploadable, store) "
                 "SELECT ?, ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM media_protections "
                 "WHERE binary_rel = ? AND object = ?)")
    parameters = (uuid.uuid4().hex, relative, name, time.time(), int(uploadable), replica_store, relative, name)
    if conn is None:
        _durable(store, statement, parameters)
    else:
        conn.execute(statement, parameters)


def _seal(store: MediaJobStore, snapshot: OriginalSnapshot, name: str) -> None:
    """Before a sidecar can carry `name`, take it out of upload reuse; the record keeps protecting it."""
    _durable(store, "UPDATE media_protections SET uploadable = 0 WHERE binary_rel = ? AND object = ?",
             (store._relative(snapshot.path), name))


def _drop_stale_holds(store: MediaJobStore, snapshot: OriginalSnapshot, keep: str, replica_store: str = "") -> None:
    """Drop the holds that an earlier attempt of this original left on a version that it does not publish now.

    A reuse takes a hold (`uploadable = 0`) on a version that another filing carries. An attempt that a crash stopped never
    drops it, and no sidecar of this original names the version, so the hold would keep it from being collected.
    """
    _durable(store, "DELETE FROM media_protections WHERE binary_rel = ? AND uploadable = 0 AND store = ? AND object <> ?",
             (store._relative(snapshot.path), replica_store, keep))


def _refuse(store: MediaJobStore, snapshot: OriginalSnapshot, refusal: MediaStoreError, ciphertext_size: int | None) -> None:
    """Record a refusal that holds for these exact bytes; the original stays local (D10)."""
    conn = store._connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO media_offload_refusals(binary_rel, sha256, code, ciphertext_size, created_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(binary_rel) DO UPDATE SET sha256=excluded.sha256, "
                "code=excluded.code, ciphertext_size=excluded.ciphertext_size, created_at=excluded.created_at",
                (store._relative(snapshot.path), snapshot.binary_sha256, refusal.code, ciphertext_size, time.time()),
            )
    finally:
        conn.close()


def _refused(
    store: MediaJobStore, snapshot: OriginalSnapshot, config: dict[str, object], *,
    usage: media_allowance.ReferenceUsage | None = None, recheck_allowance: bool = True, census: bool = True,
) -> str | None:
    conn = store._connect(readonly=True)
    try:
        row = conn.execute("SELECT sha256, code, ciphertext_size FROM media_offload_refusals "
                           "WHERE binary_rel = ? AND sha256 = ?",
                           (store._relative(snapshot.path), snapshot.binary_sha256)).fetchone()
    finally:
        conn.close()
    if row is None or (not recheck_allowance and row["code"] in media_allowance.ALLOWANCE_REFUSAL_CODES):
        return None
    return _refusal_code(store.vault_root, row, snapshot, config, usage=usage, census=census)


def _refusal_code(
    vault: Path, row, snapshot: OriginalSnapshot, config: dict[str, object], *,
    usage: media_allowance.ReferenceUsage | None = None, census: bool = True,
) -> str | None:
    if row["sha256"] != snapshot.binary_sha256:
        return None
    # These refusal codes are fixed by the media protocol, not inferred from error prose.
    if row["code"] == "MEDIA_OBJECT_TOO_LARGE":
        bound = config.get("max_object_bytes")
        return row["code"] if bound is not None and row["ciphertext_size"] is not None and row["ciphertext_size"] > int(bound) else None
    if row["code"] == RESIDENT_CODE:
        # Larger configured bounds release a resident original without an explicit retry.
        return row["code"] if _resident(vault, snapshot, config) else None
    if row["code"] in media_allowance.ALLOWANCE_REFUSAL_CODES:
        if snapshot.reference is not None:
            return None
        if usage is None and not census:
            return row["code"]  # the caller may not take a census: the refusal stands until one is taken
        decision, _reuse = _offload_admission(vault, snapshot, usage=usage)
        return decision.code
    return row["code"]


def _require_allowance(ledger: MediaJobStore, snapshot: OriginalSnapshot, code: str | None) -> None:
    if code is not None:
        # The worker logs this text and a status returns it whole, so it names no note: the notes behind an
        # unknown census are only in the status's structured `blocked_by`, which egress filters entry by entry.
        refusal = MediaStoreError(code, "the current media allowance does not admit this original")
        _refuse(ledger, snapshot, refusal, None)
        raise refusal


def retry_offload(vault: Path, path: Path) -> int:
    """Explicit retry releases this input's terminal refusal without replacing extraction."""
    if originals.binding_for(vault) is None:
        return 0
    ledger = MediaJobStore(vault)
    conn = ledger._connect()
    try:
        with conn:
            conn.execute("DELETE FROM media_offload_refusals WHERE binary_rel = ?", (ledger._relative(path),))
    finally:
        conn.close()
    return enqueue_offload(vault, path)


@contextmanager
def _offload_input(vault: Path, path: Path, ledger: MediaJobStore):
    snapshot = original_state(vault, path)
    try:
        yield snapshot
    except MediaStoreError as error:
        # Park repeated verification failures for these bytes; explicit retry or a changed SHA recovers them.
        if error.code == "MEDIA_ORIGINAL_UNVERIFIED" and snapshot.binary_sha256 is not None:
            _refuse(ledger, snapshot, error, None)
        raise


def offload_refusals(vault: Path, *, path: str | None = None) -> tuple[list[dict[str, object]], dict[str, object]]:
    """Originals that stay local because the store refused their exact bytes."""
    from .writer_lease import get_manager

    query = "SELECT binary_rel, sha256, code, ciphertext_size FROM media_offload_refusals"
    parameters: tuple[object, ...] = ()
    if path is not None:
        query += " WHERE binary_rel = ?"
        parameters = (path,)
    rows, coverage = _status_candidates(vault, "media_offload_refusals", query + " ORDER BY binary_rel LIMIT ?", parameters)
    if path is not None:
        coverage = {**coverage, "coverage": "exact-path"}
    config = originals.configuration(vault) or {}
    result = []
    with get_manager().consistency_guard(vault, operation="media_refusal_status", holder_kind="background"):
        usage = (media_allowance.reference_usage(vault)
                 if any(row["code"] in media_allowance.ALLOWANCE_REFUSAL_CODES for row in rows) else None)
        for row in rows:
            try:
                snapshot = original_state(vault, row["binary_rel"])
            except (OSError, ValueError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
                result.append(_unavailable_original(row["binary_rel"]))
                continue
            code = _refusal_code(vault, row, snapshot, config, usage=usage)
            if code is None:
                continue
            entry = {"path": row["binary_rel"], "original_state": snapshot.state,
                     "code": code, "ciphertext_size": row["ciphertext_size"]}
            if code in media_allowance.ALLOWANCE_REFUSAL_CODES and usage is not None:
                entry.update(media_allowance.blocked_fields(usage.blockers))
            result.append(entry)
    return result, coverage


def _finish_reference(ledger: MediaJobStore, snapshot: OriginalSnapshot, source: reserved_paths.GenericBinarySource | None = None) -> None:
    assert snapshot.reference is not None
    conn = ledger._connect()
    try:
        with conn:
            if snapshot.state == "cached":
                assert source is not None
                _record_cache(conn, ledger, snapshot, source)
            conn.execute("DELETE FROM media_protections WHERE binary_rel = ? AND object = ?",
                         (ledger._relative(snapshot.path), snapshot.reference.object))
            conn.execute("DELETE FROM media_offload_refusals WHERE binary_rel = ?", (ledger._relative(snapshot.path),))
    finally:
        conn.close()


@contextmanager
def _ciphertext_reserved(ledger: MediaJobStore, token: str, config: dict[str, object], ciphertext_size: int):
    """Hold the temporary ciphertext's bytes against the hydration capacity, which it shares and takes no slot of (D12, choice 3).

    A policy with no capacity reserves nothing, as before the capacity existed.
    """
    limits = media_capacity.hydration_limits(config)
    if limits is not None:
        _reserve(ledger, token, ciphertext_size, limits, hydration=False)
    try:
        yield
    finally:
        _release(ledger, token)


def _send_staged(backend: Store, name: str, ciphertext: BinaryIO, head: ObjectHead, *,
                 transfer: Transfer | None = None, copied_from: StoredObject | None = None) -> str:
    with _transport("upload"):
        if transfer is not None:
            return transfer.send(name, ciphertext, head, copied_from)
        return backend.upload(name, ciphertext, size=head.size, sha256=head.sha256)


def _upload_encrypted(ledger: MediaJobStore, token: str, config: dict[str, object], backend: Store, name: str,
                      plaintext: BinaryIO, plaintext_size: int, *, transfer: Transfer | None = None) -> tuple[ObjectHead, str]:
    """Encrypt `plaintext` to the vault's recipient and upload it as `name`; return the ciphertext's head and version."""
    with _ciphertext_reserved(ledger, token, config, media_capacity.age_ciphertext_size(plaintext_size)):
        with tempfile.TemporaryFile() as ciphertext:
            result = subprocess.run(["age", "--encrypt", "--recipient", str(config["recipient"])],
                                    stdin=plaintext, stdout=ciphertext, stderr=subprocess.PIPE, check=False)
            if result.returncode:
                raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "age encryption failed")
            ciphertext.seek(0)
            head = _digest(ciphertext)
            ciphertext.seek(0)
            version = _send_staged(backend, name, ciphertext, head, transfer=transfer)
            return head, version


def _confirm_publication(
    held: ExitStack, backend: Store, reference: StoredObject,
) -> _StoredVersionWitness | None:
    if _personal_filesystem(backend):
        failure, witness = _hold_checked_version(held, backend, reference, personal=True)
        if failure is None:
            return witness
    elif _confirmed(backend, reference):
        return None
    raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the uploaded version is not confirmed")


def _recheck_publication(vault: Path, witness: _StoredVersionWitness | None) -> None:
    if witness is not None:
        wait_store_read(vault, witness.store.name, witness, timeout=_PERSONAL_EVICTION_PROBE_SECONDS)


def offload(vault: Path, path: Path, *, batch: OffloadBatch | None = None,
            job: MediaJob | None = None) -> StoredObject | None:
    """Protect, encrypt, confirm and durably publish a bound original.

    None means that offload published no copy. Notes and datasets stay local, and a damaged pointer goes to repair.
    Quota refusals raise so that the worker retains retryable demand (D10).
    A `batch` shares its census across members. A direct call checks its census again before publication.
    """
    direct = batch is None
    if batch is None:
        batch = OffloadBatch(vault, record_status=False)
    batch.begin()
    try:
        return _offload_member(Path(vault).resolve(), path, batch, direct=direct, job=job)
    except BaseException:
        batch.failed()
        raise


def _offload_member(vault: Path, path: Path, batch: OffloadBatch, *,
                    direct: bool, job: MediaJob | None = None) -> StoredObject | None:
    from . import media_replicas

    config = _settings(vault)
    if config.get("enabled") is not True:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "offload is disabled")
    backend = _configured_store(vault, config)
    ledger = MediaJobStore(vault)
    def census() -> media_allowance.ReferenceUsage:
        return batch.usage(ledger)
    with _pin(vault, path) as (_ledger, token), _offload_input(vault, path, ledger) as snapshot:
        if snapshot.binary_sha256 is None or snapshot.binary_size is None:
            raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original has no exact sidecar binding")
        # The current allowance is assessed after the worker's one slice of reusable versions.
        refusal = _refused(ledger, snapshot, config, recheck_allowance=False)
        if refusal is not None:
            return None
        if not snapshot.offloadable:
            return None
        if snapshot.reference is not None:
            reference = snapshot.reference
            # A replay after a lost acknowledgement can reach a bound version. Keep it bound so repair can share
            # one replacement across all its filings without charging the allowance for a second version.
            if media_replicas.has_quarantined(vault, (reference,)):
                return None
            with ExitStack() as stack:
                personal = _personal_filesystem(backend)
                failure, witness = _hold_checked_version(stack, backend, reference, personal=personal)
                if failure is not None:
                    _quarantine_found(vault, [ledger._relative(path)], reference, failure)
                    return None
                source = (stack.enter_context(_open_local_original(vault, ledger._relative(path), snapshot))
                          if snapshot.state == "cached" else None)
                if source is not None:
                    _bind_pin(ledger, token, snapshot, source, job=job)
                    if _digest(source.stream) != ObjectHead(snapshot.binary_size, snapshot.binary_sha256):
                        raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original differs from its binding")

                def confirm() -> None:
                    _require_current_local_policy(vault, config, backend)
                    if source is not None:
                        source.require_current()
                    if original_state(vault, path) != snapshot:
                        raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the binding changed")
                    _recheck_publication(vault, witness)
                    _publish_reference(vault, snapshot, reference)
                    _finish_reference(ledger, original_state(vault, path), source)

                _publishing(vault, "media_store_confirm_reference", confirm, publishing=(reference,))
            return reference
        if snapshot.state != "local":
            raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original bytes are absent")
        if _resident(vault, snapshot, config):
            # Refused before any temporary file exists: the rule needs only the sidecar's size.
            _refuse(ledger, snapshot, MediaStoreError(RESIDENT_CODE, "the original stays on the volume"),
                    media_capacity.age_ciphertext_size(snapshot.binary_size))
            return None
        # One bounded slice from the current census. A fresh copy still needs room under the allowance.
        usage, candidates, damaged = _reuse_walk_inputs(vault, snapshot, census, str(config["key_version"]))
        reuse = None
        binary = _binding_of(vault, snapshot)
        for candidate_path, candidate in candidates:
            try:
                live = _live_filing(vault, binary, candidate_path, candidate)
            except _FILING_UNREADABLE:
                usage = media_allowance.ReferenceUsage(None, usage.observed_at)  # the census no longer vouches for its filings
                break
            failure = _head_failure(backend, candidate)
            if failure is None:
                reuse = live
                break
            usage.skip_reuse(binary, candidate)
            # A version whose bytes differ from its record is a finding. One that the store lost or condemned is passed over
            # while its filing still has bytes here: eviction releases that pointer itself. A lost version of a filing that
            # has none is a finding too, and the new version that this offload publishes heals it (`note_repair`).
            if failure not in (_ABSENT, _CONDEMNED) or (failure == _ABSENT and live.state not in _BYTES_HERE):
                _quarantine_found(vault, [ledger._relative(live.path)], candidate, failure)
            damaged.append(candidate)
        decision, _none = _offload_admission(vault, snapshot, reference=reuse.reference if reuse is not None else None,
                                             select_reuse=False, usage=usage)
        _require_allowance(ledger, snapshot, decision.code)
        key_version = str(config["key_version"])
        with (_open_local_original(vault, path.relative_to(vault).as_posix(), snapshot) as source,
              ExitStack() as destination):
            _bind_pin(ledger, token, snapshot, source, job=job)
            if _digest(source.stream) != ObjectHead(snapshot.binary_size, snapshot.binary_sha256):
                raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original differs from its binding")
            source.stream.seek(0)
            with _guard(vault, "media_store_protect"):
                source.require_current()
                if original_state(vault, path) != snapshot:
                    raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the binding changed")
                # Reuse requires a live binding at the exact protection commit.
                if reuse is not None and original_state(vault, reuse.path) != reuse:
                    reuse = None
                decision, _candidate = _offload_admission(
                    vault, snapshot, reference=reuse.reference if reuse is not None else None, select_reuse=False,
                    usage=census(),
                )
                _require_allowance(ledger, snapshot, decision.code)
                name = reuse.reference.object if reuse is not None else _upload_name(ledger, snapshot)
                _drop_stale_holds(ledger, snapshot, name)
                _protect(ledger, snapshot, name, uploadable=reuse is None)
            if reuse is not None:
                reference = reuse.reference
                assert reference is not None
            else:
                try:
                    head, version = _upload_encrypted(ledger, token, config, backend, name, source.stream,
                                                      snapshot.binary_size)
                except ObjectTooLarge as refusal:
                    _refuse(ledger, snapshot, refusal, refusal.size)
                    return None
                reference = StoredObject(1, str(config["store"]), key_version, name, version, head.size,
                                         head.sha256)
            witness = _confirm_publication(destination, backend, reference)
            def publish() -> None:
                source.require_current()
                if original_state(vault, path) != snapshot:
                    raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the binding changed")
                if direct:
                    # A file restored outside the runtime can add a pointer without a ledger marker.
                    batch.end()
                publishing_usage = census()
                decision, _candidate = _offload_admission(
                    vault, snapshot, reference=reference, select_reuse=False, usage=publishing_usage,
                )
                _require_allowance(ledger, snapshot, decision.code)
                _require_current_local_policy(vault, config, backend)
                # Charged before the write: a failed write leaves the batch over-counting, never under.
                batch.charge(ledger, snapshot, reference, publishing_usage)
                _recheck_publication(vault, witness)
                _seal(ledger, snapshot, reference.object)
                _publish_reference(vault, snapshot, reference)
                _note_replica(vault, path, reference, None if reuse is not None else time.time())
                if damaged:
                    # The filings that still list a damaged version of these bytes copy this one (`_repaired_sibling`).
                    media_replicas.note_repair(vault, ledger._relative(path), damaged)
                _finish_reference(ledger, original_state(vault, path), source)

            _publishing(vault, "media_store_publish", publish, publishing=(reference,) if reuse is not None else ())
        return reference


# The finding of a quarantined replica that no copy can replace: neither the local original nor another entry verifies.
NO_VERIFIED_SOURCE = "no_verified_source"
# A replica copy that these codes refuse meets the same refusal when it is tried again: its bytes or its target decide.
# Any other failure is an outage or a change that a later attempt can pass. Closed codes that this module raises.
# nosemgrep: ep-word-set -- the closed set of refusal codes that replicate raises; they are not words a person typed.
REPLICA_REFUSAL_CODES = frozenset({
    "MEDIA_OBJECT_TOO_LARGE", "MEDIA_ORIGINAL_UNVERIFIED", "MEDIA_REPLICA_STORE_UNKNOWN", "MEDIA_REPLICA_STORE_REFUSED",
    NO_VERIFIED_SOURCE,
})


def require_replica_store(vault: Path, name: str, *, serves_eviction: bool = False) -> originals.StoreEntry:
    """The declared store `name`, when it can take a replica now; else `MediaStoreError` says why.

    The store that serves eviction takes a replica only from `serves_eviction=True`: a copy again of its pointer.
    """
    return _require_replica_entry(_settings(vault), name, serves_eviction=serves_eviction)


def _require_replica_entry(config: dict[str, object], name: str, *, serves_eviction: bool) -> originals.StoreEntry:
    if config.get("enabled") is not True:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the media store is disabled")
    entry = originals.store_entry(config, name)
    if entry is None:
        raise MediaStoreError("MEDIA_REPLICA_STORE_UNKNOWN", "no store of that name is declared")
    if entry.serves_eviction and not serves_eviction:
        raise MediaStoreError("MEDIA_REPLICA_STORE_REFUSED",
                              "offload fills the store that serves eviction; name another declared store")
    if not entry.available:
        fields = ", ".join((*entry.missing, *entry.invalid))
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", f"the store's descriptor is incomplete: {fields}")
    if (refusal := connection_refusal(entry)) is not None:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", refusal)
    return entry


def store_reachable(vault: Path, name: str, deadline: float | None = None) -> bool | None:
    """Whether the store answers as itself now. None means the adapter has no cheap identity check."""
    from .media_b2_store import B2ReplicaStore

    try:
        backend = store_for(vault, name)
        if isinstance(backend, B2ReplicaStore):
            backend.require_identity(deadline=deadline)
        elif isinstance(backend, FilesystemStore):
            backend.require_identity()
        else:
            return None
    except (MediaStoreError, ValueError):
        return False
    return True


def _copy_backend(
    vault: Path, name: str, *, serves_eviction: bool = False, policy_copy: bool = False,
) -> tuple[dict[str, object], originals.StoreEntry, Store]:
    """Validate destination setup and confirm identity before reading or protecting the source."""
    from .media_b2_store import B2ReplicaStore

    try:
        config = _settings(vault)
        if policy_copy:
            from .media_protection_budget import PolicyTransferUnsupported

            target = originals.store_entry(config, name)
            if (target is None or target.serves_eviction
                    or target.connection.get("adapter") not in (FILESYSTEM_ADAPTER, FILESYSTEM_ADAPTER_ALIAS)):
                raise PolicyTransferUnsupported()
        entry = _require_replica_entry(config, name, serves_eviction=serves_eviction)
        backend = _configured_store(vault, config, name)
    except MediaStoreError as error:
        if error.code != "MEDIA_STORE_UNAVAILABLE":
            raise
        raise CopyStoreUnavailable(error.reason) from error
    if isinstance(backend, (FilesystemStore, B2ReplicaStore)):
        from . import media_replicas

        until = time.monotonic() + STORE_PROBE_SECONDS
        check = (partial(backend.require_identity, deadline=until) if isinstance(backend, B2ReplicaStore)
                 else backend.require_identity)
        try:
            wait_store_read(vault, name, check, timeout=STORE_PROBE_SECONDS)
        except StoreReadPending:
            raise
        except MediaStoreError as error:
            raise CopyStoreUnavailable() from error
        media_replicas._note_reach(vault, name, True)
    return config, entry, backend


def replica_unusable(vault: Path, replica: StoredObject) -> str | None:
    """Why automatic repair cannot read `replica` now, or None when it can."""
    return unreadable_reason(vault, replica.store, replica.key_version)


def unreadable_reason(vault: Path, store: str, key_version: str) -> str | None:
    """Why this install cannot download and decrypt a replica in `store` made under `key_version`, or None when it can.

    It needs a store that is available and `immediate`, and the decryption identity of the replica's key version.
    This install holds one identity, so a replica of another key version stays for the owner. Repair and the sampled
    check both read a replica only where this is None.
    """
    config = _settings(vault)
    return replica_readability(config, originals.store_entry(config, store), key_version)


IDENTITY_UNREADABLE = "identity_unreadable"


def replica_readability(config: dict[str, object], entry: originals.StoreEntry | None, key_version: str) -> str | None:
    """The read refusal, from an already read vault configuration and store descriptor."""
    if entry is None or not entry.available or connection_refusal(entry) is not None:
        return "store_unavailable"
    if entry.retrieval_class != originals.IMMEDIATE:
        return "owner_restores"
    identity = config.get("identity_path")
    if key_version != str(config.get("key_version")) or not isinstance(identity, str) \
            or not os.access(identity, os.R_OK):
        return IDENTITY_UNREADABLE
    return None


def _note_replica(vault: Path, path: Path, replica: StoredObject, verified_at: float | None,
                  copied_from: StoredObject | None = None) -> None:
    """Record a published replica in the derived table. A failure costs the row only: the next rebuild restores it."""
    from . import media_replicas

    try:
        media_replicas.record(vault, MediaJobStore(vault)._relative(path), replica, verified_at=verified_at,
                              copied_from=copied_from)
    except (sqlite3.Error, OSError) as error:
        log.warning("a published replica is not yet in the replica table: %s", type(error).__name__)


@contextmanager
def _copy_ciphertext(ledger: MediaJobStore, token: str, config: dict[str, object],
                     source: StoredObject, source_backend: Store) -> Iterator[tuple[ObjectHead, BinaryIO]]:
    """Stage `source` and hold its reserved space until the caller finishes with it.

    The bytes are hashed on the way and must match the entry's recorded size and digest, which its own upload
    confirmed; a difference raises `ReplicaMismatch`. The key is not needed, and the plaintext never exists here.
    """
    with _ciphertext_reserved(ledger, token, config, source.ciphertext_size):
        with tempfile.TemporaryFile() as ciphertext:
            digest, size = hashlib.sha256(), 0
            with closing(_stored_chunks(source_backend, source)) as chunks:
                for chunk in chunks:
                    digest.update(chunk)
                    size += len(chunk)
                    ciphertext.write(chunk)
            head = ObjectHead(size, digest.hexdigest())
            if head.size != source.ciphertext_size:
                raise ReplicaMismatch("size_mismatch")
            if head.sha256 != source.ciphertext_sha256:
                raise ReplicaMismatch("digest_mismatch")
            ciphertext.seek(0)
            yield head, ciphertext


def replicate(vault: Path, path: Path, store_name: str, *, policy: ProtectionPolicy | None = None,
              expected_companion_hash: str | None = None) -> StoredObject:
    """Make a verified replica of one bound original in the replica store `store_name`, and list it in its binding.

    The replica is ciphertext. An original with bytes on the volume is hashed against its binding, then encrypted to
    the vault's recipient. An evicted original is copied as the ciphertext that the eviction store holds, hashed on
    the way against its pointer. Either way the target store confirms the ciphertext's size and digest before the
    binding lists the replica. The sidecar write holds the vault boundary; hashing, encrypting and uploading do not.
    An existing replica in this store that still verifies is kept and nothing is written.
    An opted-in policy copy rechecks its planned filing and reserves only at ciphertext transfer.
    """
    from . import media_protection, media_protection_budget

    if ((policy is None and expected_companion_hash is not None)
            or (policy is not None and not originals.is_sha256(expected_companion_hash))):
        raise ValueError("policy copies require a policy and an exact companion hash")
    vault = Path(vault).resolve()
    config, _, backend = _copy_backend(vault, store_name, policy_copy=policy is not None)
    policy_identity = None
    if policy is not None:
        if not isinstance(backend, FilesystemStore) or store_name not in policy.replica_stores:
            raise media_protection_budget.PolicyTransferUnsupported()
        try:
            matches = media_protection.owner_policy(config) == policy
        except media_protection.ProtectionPolicyError:
            matches = False
        if not matches:
            raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the owner policy changed")
        policy_identity = backend.policy_identity()
    ledger = MediaJobStore(vault)
    with _pin(vault, path) as (_ledger, token), ExitStack() as held:
        snapshot = original_state(vault, path)
        if policy is not None and snapshot.sidecar_hash != expected_companion_hash:
            raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the planned companion changed")
        if snapshot.binary_sha256 is None or snapshot.binary_size is None:
            raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original has no exact sidecar binding")
        listed = next((replica for replica in snapshot.replicas if replica.store == store_name), None)
        if listed is not None:
            head = _verified_head(backend, listed)
            if head is not None and not head.condemned:
                return listed
            # Only a definitive absence replaces the entry; an unknown answer raised above and keeps it.
        source = None
        if snapshot.state == "evicted":
            assert snapshot.reference is not None  # its ciphertext is copied from an entry that is not damaged, below
        elif snapshot.state in ("local", "cached"):
            source = held.enter_context(_open_local_original(vault, path.relative_to(vault).as_posix(), snapshot))
            _bind_pin(ledger, token, snapshot, source)
            if _digest(source.stream) != ObjectHead(snapshot.binary_size, snapshot.binary_sha256):
                raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original differs from its binding")
            source.stream.seek(0)
        else:
            raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original bytes are absent")
        def require_current() -> None:
            if policy is not None:
                try:
                    matches = originals.configuration(vault) == config
                except (OSError, ValueError, RecursionError):
                    matches = False
                if not matches:
                    raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the owner policy or its store declaration changed")
            if source is not None:
                source.require_current()
            if original_state(vault, path) != snapshot:
                raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the binding changed")

        transfer = None
        recovered = None
        if policy is not None:
            assert isinstance(backend, FilesystemStore) and policy_identity is not None
            transfer = media_protection_budget.Transfer(
                ledger, token, backend, policy_identity, config, policy, snapshot, require_current,
            )
            recovered = transfer.replay(held)
        with _guard(vault, "media_store_replica_protect"):
            require_current()
            if recovered is not None:
                name = recovered[0].replica.object
            else:
                name = _upload_name(ledger, snapshot, store_name) if transfer is None else secrets.token_hex(32)
            if transfer is None or recovered is not None:
                _protect(ledger, snapshot, name, uploadable=transfer is None, replica_store=store_name)
        copied = None
        if recovered is not None:
            attempt, witness = recovered
            replica, copied = attempt.replica, attempt.copied_from
        else:
            if source is not None:
                key_version = str(config["key_version"])
                head, version = _upload_encrypted(ledger, token, config, backend, name, source.stream,
                                                  snapshot.binary_size, transfer=transfer)
            else:
                head, version, copied = _copy_from_replica(vault, ledger, token, config, backend, name, snapshot,
                                                           store_name, transfer=transfer)
                key_version = copied.key_version
            replica = StoredObject(1, store_name, key_version, name, version, head.size, head.sha256)
            witness = (transfer.confirm(replica, held) if transfer is not None
                       else _confirm_publication(held, backend, replica))

        def publish() -> None:
            _require_current_local_policy(vault, config, backend)
            require_current()
            _recheck_publication(vault, witness)
            _seal(ledger, snapshot, name)
            _commit_reference(vault, prepare_replica(vault, snapshot, replica))
            _note_replica(vault, path, replica, time.time(), copied)
            _durable(ledger, "DELETE FROM media_protections WHERE binary_rel = ? AND object = ?",
                     (ledger._relative(path), name))

        _publishing(vault, "media_store_replica_publish", publish, publishing=(replica,), copied_from=copied)
    return replica


def _entry_in(snapshot: OriginalSnapshot, store: str) -> StoredObject | None:
    """The entry that a binding lists for `store`: its pointer when `store` serves eviction, else its replica."""
    return next((entry for entry in (snapshot.reference, *snapshot.replicas)
                 if entry is not None and entry.store == store), None)


def _repoint_cache(vault: Path, ledger: MediaJobStore, snapshot: OriginalSnapshot, old: StoredObject,
                   new: StoredObject) -> None:
    """After a pointer is replaced, the cache row that names `old` names `new`, and a refusal about `old` is stale.

    The row keys on the pointer (`_recorded_identity`): left as it was, a cached original would never match its
    cache again and eviction would keep refusing it.
    """
    from . import media_bindings

    _durable(ledger, "UPDATE media_cache SET reference = ? WHERE binary_rel = ? AND reference = ?",
             (_reference_key(new), media_bindings.snapshot_key(vault, snapshot), _reference_key(old)))
    _durable(ledger, "DELETE FROM media_offload_refusals WHERE binary_rel = ?", (ledger._relative(snapshot.path),))


def _copy_from_replica(vault: Path, ledger: MediaJobStore, token: str, config: dict[str, object], backend: Store,
                       name: str, snapshot: OriginalSnapshot, target: str, *,
                       transfer: Transfer | None = None) -> tuple[ObjectHead, str, StoredObject]:
    """Copy the ciphertext of another entry of the binding into `backend`: the head, the version and the entry copied.

    Each entry that is not quarantined is tried in the binding's order, and only one in an `immediate` store: the
    store that holds it must be one that repair and the sampled check read too. One whose bytes differ from its record
    is quarantined as the finding it is, and the next is tried. A store that cannot answer ends the attempt as an outage
    when no entry copied; only when every entry was tried does the original have no verified source.
    """
    from . import media_replicas

    relative = ledger._relative(snapshot.path)
    outage: MediaStoreError | None = None
    candidates = tuple(candidate for candidate in (snapshot.reference, *snapshot.replicas)
                       if candidate is not None and candidate.store != target)
    damaged = media_replicas.damaged_among(vault, candidates)
    stores = originals.store_entries(config)
    store_ids = media_replicas.declared_store_ids(vault, (candidate.store for candidate in candidates))
    for candidate in candidates:
        if (candidate.store, candidate.object, candidate.version) in damaged:
            continue
        holder = stores.get(candidate.store)
        if holder is None or not holder.available or holder.retrieval_class != originals.IMMEDIATE:
            continue  # a retrieval that is not immediate is the owner's to ask for, never a background read
        with ExitStack() as staged:
            try:
                source_backend = _entry_adapter(holder)
                if isinstance(source_backend, FilesystemStore):
                    source_backend.store_id = store_ids.get(candidate.store)
                    try:
                        source_backend._validate_locator(candidate.object, candidate.version)
                    except ValueError:
                        continue  # this candidate's locator cannot name an object in its store
                head, ciphertext = staged.enter_context(_copy_ciphertext(ledger, token, config, candidate, source_backend))
            except (ReplicaAbsent, ReplicaMismatch) as finding:
                _quarantine_found(vault, [relative], candidate, finding.failure, entry=holder)
                continue
            except MediaStoreError as error:
                if error.code != "MEDIA_STORE_UNAVAILABLE":
                    raise
                outage = error
                continue
            version = _send_staged(backend, name, ciphertext, head, transfer=transfer, copied_from=candidate)
            return head, version, candidate
    if outage is not None:
        raise outage
    raise MediaStoreError(NO_VERIFIED_SOURCE, "no copy of the original verifies now")


def _repaired_sibling(vault: Path, snapshot: OriginalSnapshot, damaged: StoredObject,
                      backend: Store) -> OriginalSnapshot | None:
    """The filing that already holds a replacement for `damaged`, if one stands for this filing's bytes, else None.

    Offload's reuse rule, for a pointer of the store serving eviction: equal bytes, a live binding, a version that is not
    damaged, and one that the store confirms and has not condemned. The key version is the caller's to weigh, once it
    knows whether the filing's own bytes verify (`recopy`). The table names the filing that published the replacement
    (`note_repair`); what it records is read again from that filing's binding and trusted no further. The caller reads
    the binding again under the vault boundary, as offload does at its protection commit.
    """
    from . import media_replicas

    via = media_replicas.repaired_via(vault, damaged)
    if via is None:
        return None
    try:
        sibling = original_state(vault, vault / via)
        reference = sibling.reference
        if (reference is None or reference == damaged or reference.store != damaged.store
                or (sibling.binary_sha256, sibling.binary_size) != (snapshot.binary_sha256, snapshot.binary_size)
                or media_replicas.has_quarantined(vault, (reference,)) or not _confirmed(backend, reference)):
            return None
    except (OSError, ValueError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError, ReplicaMismatch):
        return None
    return sibling


def recopy(vault: Path, path: Path, store_name: str) -> StoredObject | None:
    """Replace the quarantined entry that a binding lists for `store_name` with a new version in the same store.

    The source is the original on the volume when its bytes hash to the binding, else another entry's ciphertext (see
    `_copy_from_replica`); with neither, it raises `no_verified_source` and the binding stays as it was. The new
    version publishes under the vault boundary only if the binding is the one read first, so an entry that a later
    write replaced is never overwritten. The damaged version stays in its store. Returns None when the binding no longer lists
    the entry that the table quarantined: that finding is stale and nothing is written.

    Several filings may list one pointer of the store serving eviction, and the allowance counts that version once. The
    finding that quarantined it quarantined them all, each with its own request (`media_replicas.quarantine_all`). A request
    whose original names a replacement that a sibling already published points to it and uploads nothing
    (`_repaired_sibling`), so a version is uploaded once for the filings that share it. A replica store has no allowance
    and no sharing: one filing, one copy.
    """
    from . import media_replicas

    vault = Path(vault).resolve()
    config, entry, backend = _copy_backend(vault, store_name, serves_eviction=True)
    ledger = MediaJobStore(vault)
    relative = ledger._relative(path)
    scope = "" if entry.serves_eviction else store_name  # the record of an upload is per store; empty serves eviction
    with _pin(vault, path) as (_ledger, token), ExitStack() as held:
        snapshot = original_state(vault, path)
        if snapshot.binary_sha256 is None or snapshot.binary_size is None:
            raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original has no exact sidecar binding")
        damaged = _entry_in(snapshot, store_name)
        if damaged is None or not media_replicas.has_quarantined(vault, (damaged,)):
            # A publication that stopped after the binding changed but before its cache row followed (`_repoint_cache`)
            # is retried as a stale finding: the one recovery that eviction waits for records the cache row now.
            if entry.serves_eviction and snapshot.state == "cached" and not cache_matches(vault, path, snapshot=snapshot):
                recover_cache(vault, path, snapshot)
            return None
        key_version = str(config["key_version"])
        sibling = _repaired_sibling(vault, snapshot, damaged, backend) if entry.serves_eviction else None
        source = None
        # A filing whose own bytes verify uploads its own copy under the current key: a replacement of an older key may be
        # one that this install cannot decrypt. Only a filing whose bytes do not verify, or that has none, takes it.
        if ((sibling is None or sibling.reference.key_version != key_version)
                and snapshot.state in _BYTES_HERE):
            try:
                # Every byte is hashed here, so a source of the wrong size is judged by its digest, not refused at the open.
                local = held.enter_context(_open_local_original(vault, relative, snapshot, check_size=False))
                whole = _digest(local.stream) == ObjectHead(snapshot.binary_size, snapshot.binary_sha256)
            except (OSError, ValueError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
                whole = False  # an original that cannot be opened or read is no source; another entry may hold the bytes
            if whole:
                local.stream.seek(0)
                _bind_pin(ledger, token, snapshot, local)
                source = local
                sibling = None
        with _guard(vault, "media_store_replica_protect"):
            if source is not None:
                source.require_current()
            if original_state(vault, path) != snapshot:
                raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the binding changed")
            if sibling is not None and original_state(vault, sibling.path) != sibling:
                raise MediaStoreError(STORE_BUSY_CODE, "the filing that holds the replacement changed")
            name = sibling.reference.object if sibling is not None else _upload_name(ledger, snapshot, scope)
            _drop_stale_holds(ledger, snapshot, name, scope)
            _protect(ledger, snapshot, name, uploadable=sibling is None, replica_store=scope)
        copied = None
        if sibling is not None:
            def drop_hold(failure: type[BaseException] | None, *_unused: object) -> None:
                # A reuse that stops before it publishes must not leave this filing holding the sibling's version, which
                # no sidecar of this filing names: the hold would keep that version from being collected.
                if failure is not None:
                    _durable(ledger, "DELETE FROM media_protections WHERE binary_rel = ? AND object = ?", (relative, name))

            held.push(drop_hold)
            replica = sibling.reference
        else:
            if source is not None:
                head, version = _upload_encrypted(ledger, token, config, backend, name, source.stream,
                                                  snapshot.binary_size)
            else:
                head, version, copied = _copy_from_replica(vault, ledger, token, config, backend, name, snapshot,
                                                           store_name)
                key_version = copied.key_version
            replica = StoredObject(1, store_name, key_version, name, version, head.size, head.sha256)
        witness = _confirm_publication(held, backend, replica)

        def publish() -> None:
            _require_current_local_policy(vault, config, backend)
            if source is not None:
                source.require_current()
            if sibling is not None and original_state(vault, sibling.path) != sibling:
                raise MediaStoreError(STORE_BUSY_CODE, "the filing that holds the replacement changed")
            _recheck_publication(vault, witness)
            _seal(ledger, snapshot, name)
            # The prepared edit re-reads the sidecar and refuses unless it is still the one that `snapshot` names.
            if entry.serves_eviction:
                # The pointer's old name retires with its reference (collect-released-media D5): the caller commits the
                # release first, so a crash between the two keeps the reference, and a hold condemns the name only
                # when no retained backup or companion still references it. A replica's name is never released: no
                # replica store collects.
                if damaged.object != name:  # a retried copy that reuses the damaged name must not release its own
                    _release_name(ledger, damaged.object)
                if sibling is None:
                    # A new version, which the census has not seen and the filings that still list `damaged` reuse. Both
                    # are noted before the write, as `OffloadBatch.charge` is: a failed write over-counts, never under.
                    _note_replacement(ledger)
                    media_replicas.note_repair(vault, relative, [damaged])
                _commit_reference(vault, prepare_reference(vault, snapshot, replica))
                _repoint_cache(vault, ledger, snapshot, damaged, replica)
            else:
                _commit_reference(vault, prepare_replica(vault, snapshot, replica))
            _note_replica(vault, path, replica, None if sibling is not None else time.time(), copied)
            _durable(ledger, "DELETE FROM media_protections WHERE binary_rel = ? AND object = ?", (relative, name))

        _publishing(vault, "media_store_replica_recopy", publish, publishing=(replica,), copied_from=copied)
    return replica


def _hydrate(vault: Path, snapshot: OriginalSnapshot, ledger: MediaJobStore, token: str) -> None:
    """Hydrate in one stream into one temporary file in the original's folder (D12, choice 3).

    The reservation names the temporary before it exists, so a stop at any point
    leaves a row that names its file, and nothing at the original's path.
    """
    from . import media_bindings, source_original_custody
    from .cli_ops import OpError

    current = original_state(vault, snapshot.path)
    if replace(current, state=snapshot.state, local_path=snapshot.local_path) != snapshot:
        raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the original changed before hydration")
    if current.state != "evicted":
        return  # Another completed hydration needs no second provider read.
    relative = ledger._relative(snapshot.path)
    # Only a filing hydrates into the pool; a Trash entry comes back as an ordinary file at its own Trash path,
    # and so does a suffix-less original, whose pool-only companion the egress census could not tell from a note.
    key = media_bindings.object_key(vault, relative, snapshot.binary_sha256)
    if key != relative and originals.may_share_bytes(relative):
        try:
            _hydrate_shared(vault, snapshot, ledger, token, key)
        except OpError as error:
            if error.code != media_capacity.UPLOAD_SPACE_BUSY:
                raise
            raise MediaStoreError(STORE_BUSY_CODE, error.message) from error
        return
    temporary = held_fs.publish_temporary_name()
    custody = None
    with ExitStack() as lifetime:
        try:
            custody = source_original_custody.prepare(vault, snapshot, ledger, token)
            _hydrate_reserved(vault, snapshot, ledger, token, temporary, lifetime=lifetime, custody=custody)
        except OpError as error:
            if error.code != media_capacity.UPLOAD_SPACE_BUSY:
                raise
            raise MediaStoreError(STORE_BUSY_CODE, error.message) from error
        finally:
            if custody is not None:
                gone = custody.finish(temporary)
            else:
                with closing(ledger._connect()) as conn:
                    row = conn.execute("SELECT source_custody_json FROM media_access WHERE token=?", (token,)).fetchone()
                # Interrupted preparation retains uncertainty, even when its owner object never returned.
                gone = bool(row is not None and row[0] is None and
                            _remove_temporary(vault, ledger._relative(snapshot.path), temporary))
            _release(ledger, token, temporary_gone=gone)


def _release(ledger: MediaJobStore, token: str, *, temporary_gone: bool = True) -> None:
    """End this pin's reservation and free its slot.

    A temporary file that this process cannot remove keeps its name and bytes on the row,
    so the clear after this process ends removes it.
    """
    conn = ledger._connect()
    try:
        with conn:
            conn.execute("UPDATE media_access SET hydrating = 0, temporary_bytes = "
                         "CASE WHEN :gone THEN 0 ELSE temporary_bytes END, temporary_name = "
                         "CASE WHEN :gone THEN NULL ELSE temporary_name END, hydration_key = "
                         "CASE WHEN :gone THEN NULL ELSE hydration_key END, temporary_identity = "
                         "CASE WHEN :gone THEN NULL ELSE temporary_identity END, source_custody_json = "
                         "CASE WHEN :gone THEN NULL ELSE source_custody_json END WHERE token = :token",
                         {"gone": temporary_gone, "token": token})
    finally:
        conn.close()


def _hydrate_shared(vault: Path, snapshot: OriginalSnapshot, ledger: MediaJobStore, token: str, key: str) -> None:
    """Reserve one physical download, verify every byte, then publish without logical binary copies."""
    from . import original_pool

    config = _settings(vault)
    assert snapshot.reference is not None and snapshot.binary_size is not None and snapshot.binary_sha256 is not None
    limits = media_capacity.hydration_limits(config)
    if limits is None:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "media hydration limits are not configured")
    temporary = reserved_paths._owner_stage_name()
    _reserve(ledger, token, snapshot.binary_size, limits, hydration=True, temporary=temporary, hydration_key=key)
    created = None
    collision = False

    def record_created(identity: held_fs.StableIdentity) -> None:
        nonlocal created
        created = identity  # Live failure cleanup retains proof even if the transaction fails.
        encoded = json.dumps((identity.device, identity.inode, identity.kind, identity.link_count))
        with closing(ledger._connect()) as conn:
            conn.execute("PRAGMA synchronous=FULL")
            with conn:
                changed = conn.execute("UPDATE media_access SET temporary_identity = ? "
                                       "WHERE token = ? AND temporary_name = ? AND hydration_key = ?",
                                       (encoded, token, temporary, key)).rowcount
                if changed != 1:
                    raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the hydration reservation changed")

    try:
        current = original_state(vault, snapshot.path)
        if replace(current, state=snapshot.state, local_path=snapshot.local_path) != snapshot:
            raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the original changed before hydration")
        if current.state != "evicted":
            return
        with closing(_decrypted_chunks(store_for(vault), snapshot.reference, str(config["identity_path"]))) as chunks:
            with original_pool.prepare(vault, snapshot.binary_sha256, snapshot.binary_size,
                    held_fs.Chunks(lambda: chunks), temporary_name=temporary, on_created=record_created,
                    meter_allocation=media_capacity.managed_allocations(vault)) as prepared:
                for _attempt in range(3):
                    try:
                        with _guard(vault, "media_store_hydrate"):
                            current = original_state(vault, snapshot.path)
                            # Another filing can publish these same bytes while this download runs.
                            if replace(current, state=snapshot.state, local_path=snapshot.local_path) != snapshot:
                                raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the original changed during hydration")
                            prepared.publish()
                            current = original_state(vault, snapshot.path)
                            if current.local_path != original_pool.object_path(vault, snapshot.binary_sha256):
                                return  # A separately verified ordinary original won its logical destination.
                            with closing(ledger._connect()) as conn, conn:
                                _record_cache(conn, ledger, current, prepared.source)
                            return
                    except original_pool.PublicationRace:
                        prepared.confirm_existing()  # Hash a concurrent winner outside the canonical guard.
                raise MediaStoreError(STORE_BUSY_CODE, "shared original publication is in use")
    except held_fs.HeldFsError as error:
        collision = created is None and error.code == "DESTINATION_EXISTS"
        raise
    except ValueError as error:
        raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original failed authenticated verification") from error
    finally:
        gone = collision
        if not collision:
            try:
                original_pool.remove_stage(vault, key, temporary, created)
                gone = True
            except (ValueError, OSError, held_fs.HeldFsError):
                pass  # Unknown or replaced stages retain cleanup custody and their byte charge.
        _release(ledger, token, temporary_gone=gone)


def _hydrate_reserved(vault: Path, snapshot: OriginalSnapshot, ledger: MediaJobStore, token: str,
                      temporary: str, *, lifetime: ExitStack, custody=None) -> None:
    from .vault import PathGuard, recheck_path_guards

    config = _settings(vault)
    assert snapshot.reference is not None and snapshot.binary_size is not None
    relative = snapshot.path.relative_to(vault)
    limits = media_capacity.hydration_limits(config)
    if limits is None:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "media hydration limits are not configured")
    _reserve(ledger, token, snapshot.binary_size, limits, hydration=True, temporary=temporary)
    backend = store_for(vault)
    guard = PathGuard.capture(vault, relative.as_posix(), leaf_policy="absent")
    filesystem = lifetime.enter_context(held_fs.acquire(vault).require())
    parent = lifetime.enter_context(filesystem.parent(relative.parent.as_posix(), access="mutate").require())
    if custody is not None:
        custody.reserve(temporary, ledger, token)
    file = lifetime.enter_context(filesystem.file(parent, temporary, access="write", create=True, exclusive=True).require())
    written = file.identity
    if custody is not None:
        custody.bind(temporary, file)
    try:
        with os.fdopen(os.dup(file.descriptor), "wb") as sink:
            destination = sink
            if media_capacity.managed_allocations(vault):
                destination = media_capacity.MeteredSink(
                    vault, sink, device=os.fstat(file.descriptor).st_dev, size=snapshot.binary_size,
                )
            plaintext = _decrypt_into(backend, snapshot.reference, str(config["identity_path"]), destination)
        os.fsync(file.descriptor)
        if plaintext != ObjectHead(snapshot.binary_size, snapshot.binary_sha256):
            raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original failed authenticated verification")
        verified_version = file.version()

        def install(owner=None):
            if original_state(vault, snapshot.path) != snapshot:
                raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the original changed during hydration")
            recheck_path_guards(vault, (guard,))
            held_fs.install_temporary(filesystem, parent, temporary, relative.name, expected_identity=written,
                expected_version=verified_version, before_install=owner.before_install if owner else None,
                after_install=owner.installed if owner else None).require()
            filesystem.flush_directory(parent).require()
            with reserved_paths.open_generic_binary(vault, relative.as_posix()) as placed:
                if (placed.version.identity.device, placed.version.identity.inode) != (written.device, written.inode):
                    raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the placed original changed")
                with closing(ledger._connect()) as conn, conn:
                    _record_cache(conn, ledger, original_state(vault, snapshot.path), placed)

        # Downloads and decryption run outside the boundary; only verified installation enters it.
        if custody is not None:
            custody.install(file, plaintext.sha256, install)
        else:
            with _guard(vault, "media_store_hydrate"):
                install()
    except BaseException:
        if custody is None:
            # The held parent still reaches an unregistered temporary after its folder moves.
            with suppress(held_fs.HeldFsError, OSError):
                with filesystem.file(parent, temporary, access="mutate").require() as remainder:
                    if remainder.identity == written:
                        filesystem.unlink(remainder).require()
        raise


def _require_transfer_time(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the sampled read exceeded its deadline")


def _decrypted_chunks(backend: Store, reference: StoredObject, identity: str, *, prove: bool = False,
                      deadline: float | None = None) -> Iterator[bytes]:
    """Stream plaintext; exhaustion settles what a failed read means. This is the one place that proves damage.

    A read that reaches the end of the ciphertext judges it against the record: a size or digest that differs from it
    is damage (`ReplicaMismatch`). With `prove`, a read whose age stops early goes on hashing the rest, so that a refusal
    is judged too: ciphertext that matches its record and that age refuses is `KeyRefused` (the identity or recipient is
    wrong, and the bytes are no worse for it). Only a sampled check and a repair prove: nothing else quarantines, so
    hydration and export stop downloading when age stops, and say only that the original did not verify. An age that
    a signal stopped says nothing about either and raises `MEDIA_STORE_UNAVAILABLE` (a platform that reports a stopped
    process as a positive code reads it as a key refusal, which damages nothing).
    """
    _require_transfer_time(deadline)
    process = subprocess.Popen(["age", "--decrypt", "--identity", identity],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert process.stdin is not None and process.stdout is not None
    ciphertext, fed, feed_errors = hashlib.sha256(), [0], []
    stopped = threading.Event()  # the reader is gone: nothing needs the rest of the download
    abandoned = []  # age stopped early and nothing asked for proof: the ciphertext was not read to its end

    def feed() -> None:
        piping = True
        try:
            stream = (_stored_chunks(backend, reference, deadline=deadline) if deadline is not None
                      else _stored_chunks(backend, reference))
            with closing(stream) as chunks:
                for chunk in chunks:
                    if stopped.is_set():
                        return
                    ciphertext.update(chunk)
                    fed[0] += len(chunk)
                    if piping:
                        try:
                            process.stdin.write(chunk)
                        except BrokenPipeError:
                            if not prove:
                                abandoned.append(True)
                                return
                            piping = False  # age stopped early; the rest is still hashed against the record
        except BaseException as error:  # noqa: BLE001 - re-raised on the calling thread below.
            feed_errors.append(error)
        finally:
            with suppress(OSError):
                process.stdin.close()

    def expire() -> None:
        stopped.set()
        with suppress(OSError):
            process.kill()

    timer = threading.Timer(max(0.0, deadline - time.monotonic()), expire) if deadline is not None else None
    if timer is not None:
        timer.daemon = True
        timer.start()
    feeder = threading.Thread(target=feed, name="media-hydration-feed", daemon=True)
    feeder.start()
    try:
        # read1 returns what age has written so far, so the file grows as the download arrives.
        while chunk := process.stdout.read1(1024 * 1024):
            yield chunk
        returncode = process.wait()
    except BaseException:
        stopped.set()
        process.kill()
        process.wait()
        raise
    finally:
        feeder.join()
        if timer is not None:
            timer.cancel()
        process.stdout.close()
    _require_transfer_time(deadline)
    if feed_errors:
        raise feed_errors[0]
    if not abandoned:
        if fed[0] != reference.ciphertext_size:
            raise ReplicaMismatch("size_mismatch")
        if ciphertext.hexdigest() != reference.ciphertext_sha256:
            raise ReplicaMismatch("digest_mismatch")
    if returncode < 0:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the decryption was stopped before it finished")
    if abandoned:
        raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the decryption stopped before the whole ciphertext was read")
    if returncode:
        raise KeyRefused()


def _decrypt_into(backend: Store, reference: StoredObject, identity: str, sink: BinaryIO | media_capacity.MeteredSink, *,
                  prove: bool = False, deadline: float | None = None) -> ObjectHead:
    """Download and decrypt in one pass; local sink failures remain local errors. `prove` is `_decrypted_chunks`'s."""
    plaintext, size = hashlib.sha256(), 0
    with closing(_decrypted_chunks(backend, reference, identity, prove=prove, deadline=deadline)) as chunks:
        for chunk in chunks:
            plaintext.update(chunk)
            size += len(chunk)
            sink.write(chunk)
        sink.flush()
    return ObjectHead(size, plaintext.hexdigest())


def write_stored_original(vault: Path, reference: StoredObject, sink: BinaryIO, *, store: str | None = None,
                          prove: bool = False, deadline: float | None = None) -> ObjectHead:
    """Decrypt one stored version into `sink` and return the plaintext's head; the export pack's read (D8).

    `store` names the declared store that holds the version, as a replica's pointer does; None reads the store that
    serves eviction. `prove` is for a read whose failure may quarantine the version: see `_decrypted_chunks`.

    The pack reads a staging copy, not the live vault, so this takes no pin, reservation or ledger row. A
    transport failure raises `MEDIA_STORE_UNAVAILABLE`, after `sink` may have received a verified prefix.
    """
    return _decrypt_into(store_for(vault, store), reference, str(_settings(vault)["identity_path"]), sink,
                         prove=prove, deadline=deadline)


def _payload_path(companion: str, binary: originals.BinaryBinding, displaced: bool) -> str:
    """Where an original's bytes belong: a live original's bound path, or a Trash payload's path beside its companion.

    A Trash companion of a deleted folder binds the path that the original had before the deletion. A
    displaced page (`media_allowance.bound_references`) sits away from its original, which stays at its bound path.
    """
    if not displaced and trash_metadata.is_trash_path(companion):
        return (PurePosixPath(companion).parent / PurePosixPath(binary.path).name).as_posix()
    return binary.path


def evicted_members(vault: Path, binding: originals.StoreBinding) -> Iterator[tuple[str, originals.BinaryBinding, StoredObject]]:
    """Each original whose bytes are not on the volume and whose version the store holds, Trash included.

    A pooled original is no member: its pool object ships as a canonical member, so the store adds nothing.
    Yields the path that the archive gives it, once however many pages bind it: a live original's bound path,
    or a Trash payload's path beside its companion. A companion that cannot be read binds nothing, so the ledger, which keeps a row
    for each evicted path, gives the version of an original behind one. Raises `MEDIA_LEDGER_UNRESOLVED`
    when such a row does not hold a version of this store with its size. The caller holds the consistency guard.
    """
    ledger = MediaJobStore(vault)
    counted: set[str] = set()
    unreadable: set[str] = set()
    yielded: set[str] = set()
    for companion, binary, reference, displaced in media_allowance.bound_references(
            vault, binding, unreadable=lambda path, _error: unreadable.add(path)):
        counted.add(ledger._relative(ledger.vault_root / binary.path))
        path = _payload_path(companion, binary, displaced)
        if path not in yielded and not _on_volume(vault, path) and not _pool_holds(vault, binary):
            yielded.add(path)
            yield path, binary, reference
    if not unreadable:
        return
    with closing(ledger._connect(readonly=True)) as conn:
        # Every row, not only the marked ones: a row whose bytes went missing before a census marked it
        # still names the version that the store holds.
        members, parameters = _logical_cache_members(vault, conn)
        rows = conn.execute(members, parameters).fetchall()
    for relative, key, sha256, size in rows:
        if relative in counted or _on_volume(vault, relative) or not any(
                companion in unreadable for companion, _data in originals.companion_candidates(vault, relative)):
            continue
        try:
            reference = originals.stored_reference(json.loads(key))
        except (ValueError, TypeError):
            reference = None
        if (reference is None or reference.store != binding.store or not isinstance(size, int) or size < 0
                or not originals.is_sha256(sha256)):
            raise MediaStoreError("MEDIA_LEDGER_UNRESOLVED", "an evicted original's ledger row holds no version to read")
        held = originals.BinaryBinding(relative, sha256, size)
        if not _pool_holds(vault, held):
            yield relative, held, reference


def _stored_chunks(backend: Store, reference: StoredObject, *, deadline: float | None = None) -> Iterator[bytes]:
    # Only backend operations are transport failures; local staging writes keep their own errors.
    from .media_b2_store import B2ReplicaStore
    from .media_edge_store import EdgeStore

    _require_transfer_time(deadline)
    if isinstance(backend, B2ReplicaStore):
        opened = backend.open(reference.object, reference.version, expected_size=reference.ciphertext_size,
                              deadline=deadline)
    elif deadline is not None and isinstance(backend, EdgeStore):
        opened = backend.open(reference.object, reference.version, deadline=deadline)
    else:
        opened = backend.open(reference.object, reference.version)
    with _transport("read"), opened as stream:
        remaining = reference.ciphertext_size
        while True:
            _require_transfer_time(deadline)
            chunk = stream.read(min(1024 * 1024, remaining + 1))
            _require_transfer_time(deadline)
            if not chunk:
                break
            if len(chunk) > remaining:
                raise ReplicaMismatch("size_mismatch")
            remaining -= len(chunk)
            yield chunk


def _bound_file(vault: Path, relative: str) -> OriginalSnapshot | None:
    """The binding of one original path, as `bound_originals` would yield it, read from its own companions only."""
    try:
        snapshot = original_state(vault, relative)
    except originals.RetirementUnconfirmed:
        raise  # Its original is unknown; an operation that left it out would read as complete.
    except (UnicodeError, ValueError):
        return None
    except reserved_paths.ReservedPathLeafError as error:
        if error.code in _UNTRUSTED_LEAF:
            return None
        raise
    return snapshot if snapshot.binary_sha256 is not None else None


def _retired(vault: Path, snapshot: OriginalSnapshot) -> bool:
    """Whether canonical Trash metadata retired the companion that binds a missing original.

    Raises `originals.RetirementUnconfirmed` when a witness names the companion but no longer confirms it.
    """
    found = originals.bound_companion(vault, snapshot.path.relative_to(vault).as_posix())
    return found is not None and trash_metadata.retired_companion(
        vault, found[0], found[1], hashlib.sha256(found[2]).hexdigest())


@contextmanager
def local_originals(vault: Path, paths: tuple[str | Path, ...], *, read: bool = False):
    """Materialize selected original bindings and pin them through a local operation.

    `read` marks an owner's download; moves, exports and recovery record no read.

    A rename or trash operation changes the held pathname, so this context keeps
    access pins rather than a read handle that must retain its original name.
    """
    if originals.binding_for(vault) is None:
        yield
        return
    root = Path(vault).resolve()
    prefixes = []
    for path in paths:
        target = Path(path)
        if not target.is_absolute():
            target = root / target
        relative = target.relative_to(root).as_posix()
        prefixes.append(relative)
        if originals.is_note_path(relative):
            prefixes.append(relative[:-3])
    folders = {prefix for prefix in prefixes if (root / prefix).is_dir()}
    def selected(relative: str) -> bool:
        return any(relative == prefix or relative.startswith(prefix + "/") for prefix in prefixes)

    seen: set[Path] = set()
    with ExitStack() as stack:
        # Only the selected bindings are resolved, so a sibling's unknown original never refuses this operation.
        # A file's companions lie beside it (`originals.companion_paths`), so it resolves alone; a folder walks.
        for snapshot in chain(
                (item for folder in sorted(folders)
                 for item in bound_originals(root, root=root / folder, selects=selected)),
                (item for prefix in prefixes
                 if prefix not in folders and (item := _bound_file(root, prefix)) is not None)):
            if snapshot.path in seen:
                continue
            seen.add(snapshot.path)
            if snapshot.state == "missing" and _retired(root, snapshot):
                # A companion retired to Trash stays at its path with no bytes and no reference: nothing to fetch
                # or pin, so the operation carries the page as it is. Any other missing original still refuses.
                continue
            stack.enter_context(pin_original(root, snapshot.path, read=read))
        yield


@contextmanager
def pin_original(vault: Path, path: str | Path, *, read: bool = False):
    """Acquire one resolved original and retain its eviction pin without keeping a file handle."""
    if originals.binding_for(vault) is None:
        yield
        return
    root = Path(vault).resolve()
    target = Path(path)
    if not target.is_absolute():
        target = root / target
    with _pin(root, target, read=read) as (ledger, token):
        snapshot = original_state(root, target)
        if snapshot.state == "evicted":
            _hydrate(root, snapshot, ledger, token)
            snapshot = original_state(root, target)
        # The pin names the physical bytes it holds, so eviction of a shared object sees every filing's pin.
        with _open_local_original(root, target.relative_to(root).as_posix(), snapshot) as source:
            _bind_pin(ledger, token, snapshot, source)
        yield


@contextmanager
def moved_originals(vault: Path, rel: str):
    """Fetch the original that moving the file `rel` carries, and pin it until the move returns.

    An original carries itself. A page carries the binding of the original beside it, which the
    move would strand: a resident one is pinned, and an evicted one is fetched only when the caller
    may see it (`originals.evicted_visible_original`), so a withheld one stays in the store and the
    page moves as if no original were there. Only that exact file is selected, so a page's name
    never selects a folder of originals beside it.
    """
    carried: str | None = rel
    if originals.is_note_path(rel):
        carried = rel[:-3]
        if not carried or originals.is_note_path(carried):
            carried = None
        elif not (Path(vault) / carried).is_file():
            stored = originals.evicted_visible_original(vault, carried)
            carried = None if stored is None else stored[1]
    if carried is None:
        yield
        return
    with local_originals(vault, (carried,)):
        yield


@contextmanager
def _open_local_original(vault: Path, relative: str, snapshot: OriginalSnapshot, *, physical: bool = False,
                         check_size: bool = True):
    """Resolve an authorized logical filing to bytes; private pool addresses stay below this boundary.

    A shared object whose size differs from its binding raises `original_pool.ObjectMismatch`, unless the caller
    hashes every byte itself and passes `check_size=False`.
    """
    if snapshot.local_path is None:
        with reserved_paths.open_generic_binary(vault, relative, physical=physical) as source:
            yield source
        return
    from . import original_pool
    from .vault import MALFORMED_FRONTMATTER_ERRORS

    assert snapshot.binary_sha256 is not None and snapshot.binary_size is not None
    bound = originals.BinaryBinding(relative, snapshot.binary_sha256, snapshot.binary_size)

    def require_binding() -> None:
        # The bound path, digest and size name the bytes; the companion's other fields, such as the
        # extraction text that a pinned job writes, may change while the bytes are held.
        try:
            current = originals.bound_companion(vault, relative)
        except MALFORMED_FRONTMATTER_ERRORS as error:
            raise reserved_paths.ReservedPathLeafError("IDENTITY_CHANGED") from error
        if (current is None or vault / current[0] != snapshot.sidecar_path
                or originals.binary_binding(current[1]) != bound):
            raise reserved_paths.ReservedPathLeafError("IDENTITY_CHANGED")
        # The pool supplies bytes only while the logical path has no resident entry.
        try:
            reserved_paths.inspect_generic_path(vault, relative)
        except reserved_paths.ReservedPathLeafError as error:
            if error.code != "MISSING":
                raise
        else:
            raise reserved_paths.ReservedPathLeafError("IDENTITY_CHANGED")

    try:
        require_binding()
        size = snapshot.binary_size if check_size else None
        with original_pool.open_object(vault, snapshot.binary_sha256, size) as source:
            def require_current() -> None:
                source.require_current()
                require_binding()

            yield reserved_paths.GenericBinarySource(
                source.stream, source.version, require_current, source.current_version,
                partial(_open_local_original, vault, relative, snapshot, physical=physical, check_size=False),
            )
            require_current()
    except held_fs.HeldFsError as error:
        raise reserved_paths.ReservedPathLeafError(error.code) from error


def pooled_head(vault: Path, relative: str, snapshot: OriginalSnapshot) -> ObjectHead:
    """The size and digest that a pooled filing's shared object holds now, read behind the filing's binding check."""
    with _open_local_original(vault, relative, snapshot) as source:
        head = _digest(source.stream)
        source.require_current()
    return head


def _read_state(vault: Path, target: str | Path, *, strict_custody: bool = False) -> OriginalSnapshot:
    """The state that a read acts on; a retirement that its witness no longer confirms refuses as unverified."""
    from .vault import MALFORMED_FRONTMATTER_ERRORS

    try:
        return original_state(vault, target, strict_companion=strict_custody)
    except originals.RetirementUnconfirmed as error:
        raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", error.reason) from error
    except MALFORMED_FRONTMATTER_ERRORS as error:
        if not strict_custody:
            raise
        raise reserved_paths.ReservedPathLeafError("IDENTITY_CHANGED") from error


@contextmanager
def open_original(vault: Path, path: str | Path, *, physical: bool = False, read: bool = False,
                  job: MediaJob | None = None, deadline: float | None = None, strict_custody: bool = False):
    """Pin before resolution; retain verified bytes through the caller's read.

    `read` marks an owner's playback or viewing; processing and imports record no read. A request passes its
    `deadline`, which bounds the hash of a cached original whose recorded identity no longer matches.
    """
    from . import original_pool

    vault = Path(vault).resolve()
    target = Path(path)
    if not target.is_absolute():
        target = vault / target
    relative = target.relative_to(vault).as_posix()
    if originals.binding_for(vault) is None:
        with ExitStack() as local:
            try:
                source = local.enter_context(reserved_paths.open_generic_binary(vault, relative, physical=physical))
            except reserved_paths.ReservedPathLeafError as error:
                if error.code != "MISSING":
                    raise
                snapshot = _read_state(vault, target, strict_custody=strict_custody)
                if snapshot.local_path is None:
                    raise error
            else:
                if job is not None:
                    if strict_custody:
                        _read_state(vault, target, strict_custody=True)
                    source.require_current()
                    # The claimed job proves the ledger exists, so this store skips the schema setup.
                    if not MediaJobStore(vault, create=False).bind_original(job, relative):
                        raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the job no longer owns these original bytes")
                yield source
                return
    with _pin(vault, target, read=read, strict_custody=strict_custody) as (ledger, token):
        snapshot = _read_state(vault, target, strict_custody=strict_custody)
        if snapshot.state == "evicted":
            _hydrate(vault, snapshot, ledger, token)
            snapshot = _read_state(vault, target, strict_custody=strict_custody)
        with ExitStack() as opened:
            try:
                source = opened.enter_context(_open_local_original(vault, relative, snapshot, physical=physical))
            except original_pool.ObjectMismatch as error:
                raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the shared original differs from its binding") from error
            _bind_pin(ledger, token, snapshot, source, job=job)
            if snapshot.reference is not None and not cache_matches(vault, target, snapshot=snapshot):
                if _digest(source.stream, deadline=deadline) != ObjectHead(snapshot.binary_size, snapshot.binary_sha256):
                    raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the cached original differs")
                source.require_current()
                conn = ledger._connect()
                try:
                    with conn:
                        _record_cache(conn, ledger, snapshot, source)
                finally:
                    conn.close()
                source.stream.seek(0)
            yield source


@contextmanager
def logical_view(vault: Path, path: str | Path, *, job: MediaJob | None = None):
    """A readable path with the original's own filename, for a decoder that dispatches on it.

    An ordinary file is its own view. Shared or evicted bytes stay pinned for the whole use and
    appear under the logical name in a private directory; nothing appears at the logical path.
    """
    vault = Path(vault).resolve()
    target = Path(path) if Path(path).is_absolute() else vault / path
    if target.is_file():
        yield target
        return
    with open_original(vault, target, job=job) as source, reserved_paths.private_copy_directory() as private:
        if target.is_file():
            yield target  # Hydration restored an unpooled original at its own path.
            return
        view = private / target.name
        local = original_state(vault, target).local_path
        try:
            if local is None:
                raise OSError("no shared object to link")
            os.symlink(local, view)
        except OSError:
            # A platform without unprivileged links copies the pinned bytes instead.
            source.stream.seek(0)
            with open(view, "xb") as sink:
                shutil.copyfileobj(source.stream, sink, 1024 * 1024)
        yield view


def hold_original(vault: Path, path: str, *, recorded_sha256: Callable[[held_fs.FileVersion], str | None],
                  deadline: float | None = None, strict_exit: bool = False):
    """Retain a logical original for checked whole-file and range delivery, including pooled bytes."""
    original = open_original(vault, path, read=True, deadline=deadline, strict_custody=strict_exit)
    return reserved_paths._hold_binary_file(vault, path, original, recorded_sha256=recorded_sha256,
                                           deadline=deadline, strict_exit=strict_exit)


def read_original_bytes(vault: Path, path: str, *, max_bytes: int | None = None) -> reserved_paths.GenericFileSnapshot:
    """Read one authorized logical original, retaining its physical version through the complete read."""
    stored = reserved_paths._read_stored_generic(vault, path, physical=False)
    if stored is not None:
        if max_bytes is not None and len(stored.data) > max_bytes:
            raise reserved_paths.ReservedPathLeafError("TOO_LARGE")
        return stored
    with open_original(vault, path) as source:
        if max_bytes is not None and source.version.size > max_bytes:
            raise reserved_paths.ReservedPathLeafError("TOO_LARGE")
        data = source.stream.read() if max_bytes is None else source.stream.read(max_bytes + 1)
        if max_bytes is not None and len(data) > max_bytes:
            raise reserved_paths.ReservedPathLeafError("TOO_LARGE")
        source.require_current()
        version = source.version
        return reserved_paths.GenericFileSnapshot(data, version.identity, version.mtime_ns / 1e9,
            (version.size, version.mtime_ns, version.change_time_ns))


# The outcomes of one eviction attempt, a closed set that `evict_to_target` branches on (D3, M3).
EVICTED, KEPT, PRUNED, UNAVAILABLE = "evicted", "kept", "pruned", "unavailable"
# A unit that needed the vault boundary found it busy (`STORE_BUSY_CODE`): ordinary contention.
BUSY = "busy"
# A claimed job keeps its original until it completes, fails or is blocked (D3, M4).
_JOB_PINS = "jobs.pinned = 1 AND jobs.state IN ('pending', 'running')"
_PINNING_JOB = f"SELECT 1 FROM jobs WHERE jobs.binary_rel = ? AND {_JOB_PINS}"


def evict(vault: Path, path: Path) -> bool:
    """Confirm outside the boundary, then exclude pins/jobs through the unlink."""
    return _evict_one(Path(vault).resolve(), path) == EVICTED


def _cached_original(vault: Path, ledger: MediaJobStore, path: Path) -> OriginalSnapshot | str:
    """The cached original that the path's cache row records, else what became of the row (D3, M3).

    A row whose path no longer holds that original, after a move or a deletion, is
    pruned, so it neither stays a candidate nor counts as room.
    """
    try:
        snapshot = original_state(vault, path)
        if snapshot.state == "cached":
            # A drifted cache identity, after a chmod for one, keeps the row and its owner read until a recovery.
            return snapshot if snapshot.offloadable and cache_matches(vault, path, snapshot=snapshot) else KEPT
    except (OSError, ValueError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
        return KEPT  # An unreadable path keeps its row; a later pass reads it again.
    return PRUNED if _prune(vault, ledger, path, snapshot) else KEPT


def _prune(vault: Path, ledger: MediaJobStore, path: Path, snapshot: OriginalSnapshot) -> bool:
    """Drop the cache row of a path that no longer holds the original it records.

    Every writer of the row pins the path or holds the vault guard, so the guard and
    the write exclusion keep them all out. An evicted path keeps its row, marked. So does a path whose bytes
    are gone and whose companion is not proven released: a damaged companion reads as no binding, and the
    row may be the census's only sign of an original that lives in the store.
    """
    relative = ledger._relative(path)
    with _guard(vault, "media_store_prune"):
        conn = ledger._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if (conn.execute("SELECT 1 FROM media_access WHERE binary_rel = ?", (relative,)).fetchone()
                    or original_state(vault, path) != snapshot):
                conn.rollback()
                return False
            binding = conn.execute("SELECT object_key FROM media_bindings WHERE binary_rel = ?", (relative,)).fetchone()
            if binding is not None and binding[0] != relative:
                from . import media_bindings

                # A representative filing cannot retire its siblings' shared cache proof.
                if snapshot.state != "evicted" or not media_bindings.current(vault, conn):
                    conn.rollback()
                    return False
                conn.execute("UPDATE media_cache SET evicted = 1 WHERE binary_rel = ? AND sha256 = ?",
                             (binding[0], snapshot.binary_sha256))
                conn.commit()
                return True
            if snapshot.state == "evicted":
                # The flag orders the claims of the original's jobs (D3, M4).
                conn.execute("UPDATE media_cache SET evicted = 1 WHERE binary_rel = ?", (relative,))
            else:
                if snapshot.state == "missing":
                    try:
                        _require_released(vault, relative)
                    except ValueError:  # The same release rule as the census; its next pass names this row.
                        # Marked, as `_fill_missing_sizes` marks a row it cannot size: the row leaves the
                        # candidates, so no pass revisits it and no budget counts its absent bytes as room.
                        conn.execute("UPDATE media_cache SET evicted = 1 WHERE binary_rel = ?", (relative,))
                        conn.commit()
                        return True  # The row left the candidates, as in the evicted branch, so paging must not count it as kept.
                conn.execute("DELETE FROM media_cache WHERE binary_rel = ?", (relative,))
            conn.commit()
            return True
        finally:
            conn.close()


def _may_evict(config: dict[str, object]) -> bool:
    """Whether the policy asks for eviction, with offload on or off.

    A cell whose edge was turned off keeps its policy and sets `enabled` false, as cellctl renders it (D12,
    the retained store). It still hydrates an owner read, so eviction must still return the copy to the store.
    """
    return config.get("eviction_enabled") is True and isinstance(config.get("enabled"), bool)


def _custody_wait(conn: sqlite3.Connection, config: dict[str, object], references: Sequence[StoredObject]) -> str | None:
    from . import media_replicas

    seconds = float(config.get("eviction_settle_seconds", 0))
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("eviction_settle_seconds must be finite and nonnegative")
    return media_replicas.eviction_custody_wait(conn, references, settled_before=time.time() - seconds)


def _before_eviction(ledger: MediaJobStore, config: dict[str, object], references: Sequence[StoredObject],
                     *, personal: bool) -> str | None:
    from . import media_replicas

    if not personal:
        return KEPT if media_replicas.has_quarantined(ledger.vault_root, references) else None
    with closing(ledger._connect(readonly=True)) as conn, conn:
        conn.execute("BEGIN")
        if media_replicas.quarantined_in(conn, references):
            return KEPT
        return _custody_wait(conn, config, references)


def _eviction_backend(vault: Path, config: dict[str, object], *, personal: bool) -> Store:
    if not personal:
        return store_for(vault)
    backend = _configured_store(vault, config)
    if not isinstance(backend, FilesystemStore):
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "personal eviction requires a filesystem content check")
    return backend


def _hold_checked_version(
    held: ExitStack, backend: Store, reference: StoredObject, *, personal: bool,
) -> tuple[str | None, _StoredVersionWitness | None]:
    if not personal:
        return _head_failure(backend, reference), None
    assert isinstance(backend, FilesystemStore)
    try:
        with _transport("check"):
            return None, held.enter_context(backend.hold_verified(reference))
    except ReplicaAbsent:
        return _ABSENT, None
    except ReplicaMismatch as mismatch:
        return mismatch.failure, None


def _eviction_store_wait(
    vault: Path, conn: sqlite3.Connection, witnesses: Sequence[_StoredVersionWitness],
) -> str | None:
    """Bound the guarded wait for the final named-store metadata probe."""
    if not witnesses:
        return None
    backend = witnesses[0].store
    row = conn.execute("SELECT store_id FROM media_store_ids WHERE store = ?", (backend.name,)).fetchone()
    if (row[0] if row is not None else None) != backend.store_id:
        return UNAVAILABLE
    try:
        wait_store_read(vault, backend.name, lambda: backend.confirm_versions(witnesses),
                        timeout=_PERSONAL_EVICTION_PROBE_SECONDS)
    except StoreReadPending:
        return BUSY
    except MediaStoreError:
        return UNAVAILABLE
    return None


@dataclass(frozen=True, slots=True)
class _DailyEviction:
    config: dict[str, object]
    high_water: float
    low_water: float

    def has_room(self, vault: Path, *, pressure: bool = True) -> bool:
        usage = shutil.disk_usage(vault)
        used_limit = self.low_water if pressure else self.high_water
        # Like upload admission, count only free blocks available to this caller.
        return usage.free >= usage.total - usage.total * used_limit


class _EvictionRefused(Exception):
    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        super().__init__(outcome)


def _personal_unlink_fence(
    vault: Path, conn: sqlite3.Connection, config: dict[str, object],
    witnesses: Sequence[_StoredVersionWitness], *, daily: _DailyEviction | None,
) -> None:
    if (waiting := _eviction_store_wait(vault, conn, witnesses)) is not None:
        raise _EvictionRefused(waiting)
    if originals.configuration(vault) != config:
        raise _EvictionRefused(POLICY_CHANGED)
    if daily is not None and daily.has_room(vault):
        raise _EvictionRefused(BELOW_TARGET)


def _evict_one(vault: Path, path: Path, *, daily: _DailyEviction | None = None) -> str:
    from . import media_protection, media_replicas

    if daily is not None:
        if originals.configuration(vault) != daily.config:
            return POLICY_CHANGED
        if daily.has_room(vault):
            return BELOW_TARGET
    config = daily.config if daily is not None else _settings(vault)
    if not _may_evict(config) or not media_protection.eviction_eligible(config):
        return KEPT
    ledger = MediaJobStore(vault)
    snapshot = _cached_original(vault, ledger, path)
    if isinstance(snapshot, str):
        return snapshot
    personal = _personal_install()
    if snapshot.local_path is not None:
        return _evict_shared(vault, snapshot, ledger, config, personal=personal, daily=daily)
    bounds = media_capacity.bounds_for(config, vault)
    assert snapshot.binary_size is not None and snapshot.reference is not None
    if bounds is None or not bounds.hydration_fits(snapshot.binary_size):
        return KEPT  # An original that could not come back stays (D3).
    identity = _identity(vault, path)
    references = (snapshot.reference,)
    if (waiting := _before_eviction(ledger, config, references, personal=personal)) is not None:
        return waiting
    with ExitStack() as held:
        try:
            backend = _eviction_backend(vault, config, personal=personal)
            failure, witness = _hold_checked_version(held, backend, snapshot.reference, personal=personal)
        except MediaStoreError as error:
            # An unavailable or unverified version cannot authorize removal; an outage also ends the pass.
            return UNAVAILABLE if error.code == "MEDIA_STORE_UNAVAILABLE" else KEPT
        if failure is not None:
            _quarantine_found(vault, [ledger._relative(path)], snapshot.reference, failure)
            return KEPT
        with _guard(vault, "media_store_evict"):
            conn = ledger._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if personal and originals.configuration(vault) != config:
                    return POLICY_CHANGED
                relative = ledger._relative(path)
                _clear_dead_access(conn, ledger, conn.execute(
                    "SELECT * FROM media_access WHERE binary_rel = ?", (relative,)).fetchall())
                if conn.execute("SELECT 1 FROM media_drain WHERE latched = 1 AND proof_version = 1").fetchone():
                    # A census found no evicted original and cellctl may now act on it, so nothing evicts again (D12).
                    # The policy read above may be stale, so the latch decides whatever it said; the next drain tick
                    # clears a latch that a served store still holds.
                    conn.rollback()
                    return KEPT
                row = conn.execute("SELECT owner_read FROM media_cache WHERE binary_rel = ?", (relative,)).fetchone()
                if (row is None
                        or row["owner_read"] is not None and time.time() - row["owner_read"] < float(config["minimum_idle_seconds"])
                        or conn.execute("SELECT 1 FROM media_access WHERE binary_rel = ?", (relative,)).fetchone()
                        or conn.execute(_PINNING_JOB, (relative,)).fetchone()
                        # A finding recorded while the store was asked: the write lock this transaction holds keeps a
                        # later one out until the unlink is done, so this is the last word before the bytes go.
                        or media_replicas.quarantined_in(conn, (snapshot.reference,))
                        or original_state(vault, path) != snapshot or _identity(vault, path) != identity):
                    conn.rollback()
                    return KEPT
                if personal and (waiting := _custody_wait(conn, config, references)) is not None:
                    return waiting
                # Pin admission and enqueue use this same SQLite write exclusion.
                # Retained parent/file handles prevent an exchanged ancestor redirect.
                target = path.relative_to(vault)
                with held_fs.acquire(vault).require() as filesystem:
                    with filesystem.parent(target.parent.as_posix(), access="mutate").require() as parent:
                        with filesystem.file(parent, target.name, access="mutate").require() as file:
                            if _identity(vault, path) != identity:
                                return KEPT
                            if witness is not None:
                                if _version_identity(file.version()) != identity:
                                    return KEPT
                                try:
                                    _personal_unlink_fence(vault, conn, config, (witness,), daily=daily)
                                except _EvictionRefused as error:
                                    return error.outcome
                                if _version_identity(file.version()) != identity or _identity(vault, path) != identity:
                                    return KEPT
                            if not media_protection.permits_unlink(vault, conn, relative, config=config):
                                return KEPT
                            filesystem.unlink(file).require()
                            filesystem.flush_directory(parent).require()
                conn.execute("UPDATE media_cache SET evicted = 1 WHERE binary_rel = ?", (relative,))
                conn.commit()
                return EVICTED
            finally:
                conn.close()


def _evict_shared(vault: Path, snapshot: OriginalSnapshot, ledger: MediaJobStore, config: dict[str, object], *,
                  personal: bool, daily: _DailyEviction | None) -> str:
    """Confirm distinct own recovery versions outside the guard; unlink once under physical pin exclusion."""
    from . import media_bindings, media_protection, original_pool

    assert snapshot.local_path is not None and snapshot.binary_sha256 is not None and snapshot.binary_size is not None
    key = snapshot.local_path.relative_to(vault).as_posix()
    binding_limit = _PERSONAL_EVICTION_MAX_BINDINGS if personal else None
    try:
        with closing(ledger._connect(readonly=True)) as conn:
            facts = media_bindings.object_facts(conn, key, binding_limit=binding_limit)
    except media_bindings.ObjectBindingProofLimit:
        return EVICTION_PROOF_LIMIT
    bounds = media_capacity.bounds_for(config, vault)
    if (facts is None or facts.resident_only or not facts.recoverable or facts.pinned
            or bounds is None or not bounds.hydration_fits(facts.size)):
        return KEPT
    if personal and len(facts.references) > _PERSONAL_EVICTION_MAX_VERSIONS:
        return EVICTION_PROOF_LIMIT
    from . import media_replicas

    if (waiting := _before_eviction(ledger, config, facts.references, personal=personal)) is not None:
        return waiting
    with original_pool.open_object(vault, facts.sha256, facts.size) as source:
        version = source.version
    with ExitStack() as held:
        witnesses = []
        try:
            backend = _eviction_backend(vault, config, personal=personal)
            for reference, owner in zip(facts.references, facts.owners, strict=True):
                failure, witness = _hold_checked_version(held, backend, reference, personal=personal)
                if witness is not None:
                    witnesses.append(witness)
                if failure is not None:
                    # Each filing names its own pointer, so the finding is recorded for the one that holds this version; the
                    # version's other filings follow it (`quarantine_all`). Nothing evicts, and the bytes here are the source
                    # of the copy again.
                    _quarantine_found(vault, [owner], reference, failure)
                    return KEPT
        except MediaStoreError as error:
            return UNAVAILABLE if error.code == "MEDIA_STORE_UNAVAILABLE" else KEPT
        with _guard(vault, "media_store_evict_shared"), closing(ledger._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if personal and originals.configuration(vault) != config:
                return POLICY_CHANGED
            _clear_dead_access(conn, ledger, conn.execute(
                "SELECT * FROM media_access WHERE object_key = ?", (key,)).fetchall())
            try:
                latest = media_bindings.object_facts(conn, key, binding_limit=binding_limit)
            except media_bindings.ObjectBindingProofLimit:
                return EVICTION_PROOF_LIMIT
            if (latest is None or latest.checkpoint != facts.checkpoint or not media_bindings.current(vault, conn)
                    or latest.sha256 != facts.sha256 or latest.size != facts.size or latest.references != facts.references
                    or latest.resident_only or not latest.recoverable or latest.pinned
                    or latest.owner_read is not None and time.time() - latest.owner_read < float(config["minimum_idle_seconds"])
                    or media_replicas.quarantined_in(conn, latest.references)):  # see `_evict_one`
                conn.rollback()
                return KEPT
            if personal and (waiting := _custody_wait(conn, config, latest.references)) is not None:
                return waiting

            def before_unlink() -> None:
                if personal:
                    _personal_unlink_fence(vault, conn, config, tuple(witnesses), daily=daily)
                if not media_protection.permits_unlink(vault, conn, key, config=config):
                    raise _EvictionRefused(KEPT)

            try:
                original_pool.evict_object(vault, facts.sha256, expected_version=version,
                                           before_unlink=before_unlink)
            except _EvictionRefused as error:
                return error.outcome
            conn.execute("UPDATE media_cache SET evicted = 1 WHERE binary_rel = ?", (key,))
            conn.commit()
            return EVICTED


# A demand lapses 15 minutes after its series last renewed it (D3).
DEMAND_LEASE_SECONDS = 15 * 60
# The personal worker can continue after one physical object's proof exceeds its handle budget.
EVICTION_PROOF_LIMIT = "binding_proof_limit"
POLICY_CHANGED, BELOW_TARGET = "policy_changed", "below_target"
# Each read takes at most this many candidates, so a pass never loads the whole cache at once.
_EVICTION_BATCH = 64
# Unread originals first, those with no pending job before those with one, each largest first;
# then owner-read originals, largest first and longest idle on ties (D3, M4). The path makes the
# order total, so a pass reads past the candidates it keeps by their count.
def _cache_selection(
    vault: Path, conn, parameters: dict[str, object], *, check_shared_bindings: bool = True,
) -> tuple[str, str, dict[str, object]]:
    """Select physical candidates without treating unknown or resident shared bytes as reclaimable room."""
    from . import media_bindings, original_pool

    physical = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'media_bindings'").fetchone()
    access = "media_access.binary_rel = media_cache.binary_rel"
    jobs = "jobs.binary_rel = media_cache.binary_rel"
    queued = "EXISTS (SELECT 1 FROM jobs WHERE jobs.binary_rel = media_cache.binary_rel AND jobs.state = 'pending')"
    lifetime = ""
    if physical:
        pooled = "substr(media_cache.binary_rel, 1, length(:pool_prefix)) = :pool_prefix"
        access = ("media_access.object_key = media_cache.binary_rel OR media_access.object_key IS NULL "
                  f"AND (media_access.binary_rel = media_cache.binary_rel OR {pooled})")
        jobs = ("jobs.object_key = media_cache.binary_rel OR jobs.object_key IS NULL "
                f"AND (jobs.binary_rel = media_cache.binary_rel OR {pooled})")
        # Two indexed lookups: an OR inside one subquery made each candidate scan every pending job. CROSS JOIN
        # fixes SQLite's loop order, so the object's few filings drive the path lookups.
        queued += (" OR EXISTS (SELECT 1 FROM media_bindings pb CROSS JOIN jobs ON jobs.binary_rel = pb.binary_rel "
                   "WHERE pb.object_key = media_cache.binary_rel AND jobs.state = 'pending')")
        lifetime = (f" AND (NOT ({pooled}) OR (:bindings_current AND "
                    "EXISTS (SELECT 1 FROM media_bindings b WHERE b.object_key = media_cache.binary_rel)")
        if check_shared_bindings:
            lifetime += (" AND NOT EXISTS (SELECT 1 FROM media_bindings b WHERE b.object_key = media_cache.binary_rel "
                         "AND (b.resident_only = 1 OR b.reference IS NULL OR b.sha256 != media_cache.sha256 "
                         "OR b.size != media_cache.size))")
        lifetime += "))"
        parameters = {**parameters, "pool_prefix": original_pool.pool_root(vault).relative_to(vault).as_posix() + "/",
                      "bindings_current": media_bindings.current(vault, conn)}
    eligible = ("FROM media_cache WHERE evicted = 0 AND size IS NOT NULL AND size <= :hydration "
                "AND (owner_read IS NULL OR owner_read <= :idle_before) "
                f"AND NOT EXISTS (SELECT 1 FROM media_access WHERE ({access})) "
                f"AND NOT EXISTS (SELECT 1 FROM jobs WHERE ({jobs}) AND {_JOB_PINS})" + lifetime)
    order = (f"ORDER BY owner_read IS NOT NULL, CASE WHEN owner_read IS NULL THEN {queued} ELSE 0 END, "
             "size DESC, owner_read, binary_rel")
    return eligible, order, parameters


def _occupancy_paths(vault: Path, conn, *, pinned: bool):
    """Count each physical job input once, keeping pending, cached and pinned bytes disjoint."""
    from . import media_bindings, original_pool

    physical = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'media_bindings'").fetchone()
    key, join = "jobs.binary_rel", ""
    parameters = {"offload": OFFLOAD_MEDIA_TYPE}
    if physical:
        key = ("COALESCE(jobs.object_key, b.object_key, jobs.binary_rel)" if pinned else
               "COALESCE(b.object_key, jobs.binary_rel)")
        join = " LEFT JOIN media_bindings b ON b.binary_rel = jobs.binary_rel"
        parameters.update(pool_prefix=original_pool.pool_root(vault).relative_to(vault).as_posix() + "/",
                          bindings_current=media_bindings.current(vault, conn))
    where = (_JOB_PINS if pinned else
             "jobs.media_type = :offload AND jobs.do_offload = 1 AND jobs.state IN ('pending', 'running')")
    query = f"SELECT {key} AS object_key FROM jobs{join} WHERE {where}"
    if physical and pinned:
        # An unresolved claimed pin conservatively retains every pooled object until acquisition.
        query += (" UNION SELECT object_key FROM media_bindings WHERE EXISTS "
                  f"(SELECT 1 FROM jobs WHERE {_JOB_PINS} AND object_key IS NULL)")
    if physical:
        pool = "substr(inputs.object_key, 1, length(:pool_prefix)) = :pool_prefix"
        retained = (f" AND (NOT ({pool}) OR (:bindings_current AND "
                    "EXISTS (SELECT 1 FROM media_bindings b WHERE b.object_key = inputs.object_key) "
                    "AND NOT EXISTS (SELECT 1 FROM media_bindings b WHERE b.object_key = inputs.object_key "
                    "AND b.resident_only = 1)))")
        pin = ("jobs.object_key = inputs.object_key OR jobs.object_key IS NULL "
               f"AND (jobs.binary_rel = inputs.object_key OR {pool})")
    else:
        retained, pin = "", "jobs.binary_rel = inputs.object_key"
    if not pinned:
        retained += f" AND NOT EXISTS (SELECT 1 FROM jobs WHERE ({pin}) AND {_JOB_PINS})"
        if physical:
            retained += (" AND NOT EXISTS (SELECT 1 FROM media_bindings b WHERE b.object_key = inputs.object_key "
                         "AND b.reference IS NULL AND NOT EXISTS (SELECT 1 FROM jobs j "
                         "WHERE j.binary_rel = b.binary_rel AND j.do_offload = 1 AND j.state IN ('pending', 'running')))")
        # A confirmed cached copy already belongs to the cache figure, even if an offload retry remains.
        retained += (" AND NOT EXISTS (SELECT 1 FROM media_cache c WHERE c.binary_rel = inputs.object_key "
                     "AND c.evicted = 0)")
    return (row[0] for row in conn.execute(
        f"WITH inputs AS ({query}) SELECT DISTINCT inputs.object_key FROM inputs WHERE 1{retained}", parameters))


@dataclass(frozen=True, slots=True)
class Occupancy:
    """What the media ledger holds on the volume that the window budget counts (D12).

    Each figure counts offloadable originals only: a resident original never leaves the volume.
    A pinned original is in `pinned_bytes` alone, so no byte is in two figures.
    """

    evictable_bytes: int
    pending_offloads: int
    pending_offload_bytes: int
    pinned_bytes: int


def _volume_bytes(vault: Path, relatives, largest_offloadable: int) -> tuple[int, int]:
    """The count and bytes of the originals that the volume holds now and that the rule calls offloadable."""
    count = total = 0
    for relative in relatives:
        try:
            size = os.lstat(vault / relative).st_size
        except FileNotFoundError:
            continue  # A moved or evicted original holds no byte on the volume.
        if media_capacity.classify(size, largest_offloadable) == media_capacity.OFFLOADABLE:
            count, total = count + 1, total + size
    return count, total


def occupancy(vault: Path, bounds: media_capacity.Bounds, *, eviction_idle_seconds: float | None) -> Occupancy:
    """Read the ledger for the window budget; raises `MediaStoreError` when it cannot be read.

    `eviction_idle_seconds` is the policy's minimum idle age, or None when eviction is off: then no
    cached original is evictable, because a window can never reuse its room. A missing ledger holds nothing.
    """
    vault = Path(vault).resolve()
    ledger = MediaJobStore(vault, create=False)
    if not ledger.path.exists():
        return Occupancy(0, 0, 0, 0)
    largest = bounds.largest_offloadable()
    try:
        with closing(ledger._connect(readonly=True)) as conn:
            evictable = 0
            if eviction_idle_seconds is not None:
                eligible, _order, parameters = _cache_selection(vault, conn,
                    {"hydration": bounds.hydration_bytes, "idle_before": time.time() - eviction_idle_seconds})
                evictable = int(conn.execute(
                    "SELECT COALESCE(SUM(size), 0) " + eligible, parameters,
                ).fetchone()[0])
            pending = _volume_bytes(vault, _occupancy_paths(vault, conn, pinned=False), largest)
            pinned = _volume_bytes(vault, _occupancy_paths(vault, conn, pinned=True), largest)[1]
    except _LEDGER_UNREADABLE as error:
        raise MediaStoreError("MEDIA_LEDGER_UNREADABLE", f"the media ledger cannot be read: {error}") from error
    return Occupancy(evictable, pending[0], pending[1], pinned)


def cached_bytes(vault: Path) -> int:
    """The bytes that the volume allocates to cached copies of stored originals (D10, D12).

    One source for "cached": a row that is not evicted is a stored original that a hydration or a confirm left
    on the volume. A row counts only while the file there is the one that it recorded; an original rewritten
    since is the vault's own bytes. Each file counts its allocated blocks, the unit of the volume's used bytes.
    Raises `MediaStoreError` when the ledger or a cached file cannot be read; a missing ledger caches nothing.
    """
    vault = Path(vault).resolve()
    ledger = MediaJobStore(vault, create=False)
    if not ledger.path.exists():
        return 0
    total = 0
    try:
        with closing(ledger._connect(readonly=True)) as conn:
            for relative, identity in conn.execute("SELECT binary_rel, identity FROM media_cache WHERE evicted = 0"):
                total += _cached_allocation(vault / relative, identity)
    except _LEDGER_UNREADABLE as error:
        raise MediaStoreError("MEDIA_LEDGER_UNREADABLE", f"the media ledger cannot be read: {error}") from error
    return total


def _cached_allocation(path: Path, identity: str) -> int:
    """The bytes that the volume allocates to one cached row's file, or 0 when the file there is not the recorded one."""
    try:
        # A symlink leaf is not followed. A symlinked parent is, which is acceptable: the identity match still decides.
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return 0  # A moved or deleted original holds no byte on the volume.
    except OSError as error:
        # Status shows this text, so it names no path.
        raise MediaStoreError("MEDIA_CACHE_UNREADABLE",
                              f"a cached original cannot be read: {errno.errorcode.get(error.errno, 'OSError')}") from None
    if not stat.S_ISREG(info.st_mode):
        return 0  # A symlink, folder or device at the path is no cached original.
    blocks = getattr(info, "st_blocks", None)  # POSIX reports allocation in 512-byte units; Windows reports none.
    if blocks is None:
        raise MediaStoreError("MEDIA_CACHE_UNREADABLE", "this platform reports no allocated size for a file")
    if _identity_text(info.st_dev, info.st_ino, info.st_ctime_ns, info.st_mtime_ns, info.st_size) != identity:
        return 0  # Rewritten or replaced since the row was recorded: these are the vault's own bytes now.
    return blocks * 512


def evicted_bytes(vault: Path) -> int:
    """The bytes of the originals that the media ledger records as evicted; raises `MediaStoreError` when it cannot say."""
    ledger = MediaJobStore(vault, create=False)
    if not ledger.path.exists():
        raise MediaStoreError("MEDIA_LEDGER_UNREADABLE", "the media ledger does not exist")
    try:
        with closing(ledger._connect(readonly=True)) as conn:
            total, unsized = conn.execute(
                "SELECT COALESCE(SUM(size), 0), COUNT(*) - COUNT(size) FROM media_cache WHERE evicted = 1").fetchone()
    except _LEDGER_UNREADABLE as error:
        raise MediaStoreError("MEDIA_LEDGER_UNREADABLE", f"the media ledger cannot be read: {error}") from error
    if unsized:
        raise MediaStoreError("MEDIA_LEDGER_UNREADABLE", f"the media ledger records no size for {unsized} evicted originals")
    return int(total)


def _retained_figures(vault: Path, config: dict[str, object]) -> tuple[int, int, int]:
    """The evicted bytes, the room below the growth threshold and the volume's size; raises when one is unknown."""
    threshold = media_capacity.growth_threshold(config)
    if threshold is None:
        raise MediaStoreError("MEDIA_SHORTFALL_UNKNOWN", "the media store policy sets no growth threshold")
    evicted = evicted_bytes(Path(vault).resolve())
    try:
        usage = shutil.disk_usage(vault)
    except OSError as error:
        raise MediaStoreError("MEDIA_SHORTFALL_UNKNOWN", f"the volume's size and use cannot be read: {error}") from error
    return evicted, max(0, int(threshold * usage.total) - usage.used), usage.total


def _retained_gap(vault: Path, config: dict[str, object], *, margin: bool) -> int:
    """The evicted bytes that do not fit below the growth threshold, less the eviction margin when `margin` is set."""
    evicted, room, total = _retained_figures(vault, config)
    if margin and evicted:
        try:
            room = max(0, room - int(float(config["eviction_margin"]) * total))
        except (KeyError, TypeError, ValueError) as error:
            raise MediaStoreError("MEDIA_SHORTFALL_UNKNOWN", "the media store policy sets no readable eviction_margin") from error
    return max(0, evicted - room)


def retained_shortfall(vault: Path, config: dict[str, object]) -> int:
    """The bytes of evicted originals that keep a retained store from draining (D12): those that do not fit below
    the growth threshold less the eviction margin, the room the drain step needs before it starts.

    The store drains when this reaches zero. Raises `MediaStoreError` when the policy, the ledger or the volume
    cannot give it: an unknown shortfall is never zero.
    """
    return _retained_gap(vault, config, margin=True)


def record_demand(vault: Path, series: str, demand_bytes: int, *, budget: int, reachable_budget: int) -> bool:
    """Renew one import series' demand for room; a demand above the reachable budget is ignored.

    The window budget falls by what the volume gains while the series waits, so the
    demand keeps the budget as an offset from current use (simplest choice, marked).
    """
    if demand_bytes > reachable_budget:
        return False  # Any status caller can send a demand, so one the cell cannot reach changes nothing.
    ledger = MediaJobStore(vault)
    conn = ledger._connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO media_demands(series, demand_bytes, budget_offset, renewed_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(series) DO UPDATE SET demand_bytes=excluded.demand_bytes, "
                "budget_offset=excluded.budget_offset, renewed_at=excluded.renewed_at",
                (series, int(demand_bytes), int(budget) + shutil.disk_usage(vault).used, time.time()),
            )
    finally:
        conn.close()
    return True


def _live_demand(ledger: MediaJobStore) -> tuple[int, int] | None:
    conn = ledger._connect()
    try:
        with conn:
            conn.execute("DELETE FROM media_demands WHERE renewed_at < ?", (time.time() - DEMAND_LEASE_SECONDS,))
            row = conn.execute("SELECT demand_bytes, budget_offset FROM media_demands "
                               "ORDER BY demand_bytes DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return None if row is None else (int(row["demand_bytes"]), int(row["budget_offset"]))


def _fill_missing_sizes(conn, ledger: MediaJobStore) -> None:
    """Give a bounded batch of rows from before the size column their size (D3, M3).

    Without it, an upgrade would keep every original cached before it on the volume.
    The size is only a candidate hint: `evict` verifies each original again.
    """
    for row in conn.execute("SELECT binary_rel FROM media_cache WHERE size IS NULL AND evicted = 0 LIMIT ?",
                            (_EVICTION_BATCH,)).fetchall():
        try:
            size = os.lstat(ledger.vault_root / row["binary_rel"]).st_size
        except FileNotFoundError:
            # An earlier eviction kept the row; the flag only orders the claim.
            conn.execute("UPDATE media_cache SET evicted = 1 WHERE binary_rel = ?", (row["binary_rel"],))
        except OSError:
            continue  # The next pass tries again; this row only stays out of the candidates.
        else:
            conn.execute("UPDATE media_cache SET size = ? WHERE binary_rel = ?", (size, row["binary_rel"]))


def _refresh_candidates(ledger: MediaJobStore) -> None:
    """Clear dead reservations, and size a batch of rows from before the size column."""
    conn = ledger._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _clear_dead_access(conn, ledger, conn.execute("SELECT * FROM media_access").fetchall())
        _fill_missing_sizes(conn, ledger)
        conn.commit()
    finally:
        conn.close()


def _candidates(ledger: MediaJobStore, parameters: dict[str, object], offset: int) -> list[tuple[str, int]]:
    conn = ledger._connect()
    try:
        eligible, order, parameters = _cache_selection(ledger.vault_root, conn, parameters)
        # The physical row owns age and size; an indexed representative supplies the logical access path.
        rows = conn.execute("SELECT COALESCE((SELECT b.binary_rel FROM media_bindings b "
                            "WHERE b.object_key = media_cache.binary_rel ORDER BY b.binary_rel LIMIT 1), binary_rel) "
                            "AS logical_rel, size " + eligible + " " + order + " LIMIT :batch OFFSET :offset",
                            {**parameters, "batch": _EVICTION_BATCH, "offset": offset}).fetchall()
        return [(row["logical_rel"], int(row["size"])) for row in rows]
    finally:
        conn.close()


def _personal_candidate_page(
    ledger: MediaJobStore, parameters: dict[str, object], after: str,
) -> list[sqlite3.Row]:
    """Seek a fixed range before consulting eligibility or resolving a shared original."""
    with closing(ledger._connect(readonly=True)) as conn:
        # The page checks for a current projection and one representative. The unlink proves every binding within its limit.
        eligible, _, parameters = _cache_selection(ledger.vault_root, conn, parameters, check_shared_bindings=False)
        return conn.execute(
            "SELECT physical_rel, CASE WHEN EXISTS (SELECT 1 " + eligible
            + " AND media_cache.binary_rel = page.physical_rel) THEN COALESCE("
            "(SELECT b.binary_rel FROM media_bindings b WHERE b.object_key = page.physical_rel "
            "ORDER BY b.binary_rel LIMIT 1), page.physical_rel) END AS logical_rel "
            "FROM (SELECT binary_rel AS physical_rel FROM media_cache "
            "WHERE binary_rel > :after ORDER BY binary_rel LIMIT :batch) page "
            "ORDER BY physical_rel",
            {**parameters, "after": after, "batch": _EVICTION_BATCH + 1},
        ).fetchall()


def _room_covers(vault: Path, ledger: MediaJobStore, parameters: dict[str, object], shortfall: int) -> bool:
    """Whether the evictable originals hold `shortfall` bytes; rows of moved originals are pruned on the way.

    The sum over the rows bounds the room from above, so a demand that it cannot cover
    reads no original.
    """
    conn = ledger._connect()
    try:
        eligible, _order, parameters = _cache_selection(vault, conn, parameters)
        if conn.execute("SELECT COALESCE(SUM(size), 0) " + eligible, parameters).fetchone()[0] < shortfall:
            return False
    finally:
        conn.close()
    room = remaining = 0
    while page := _candidates(ledger, parameters, remaining):
        for relative, size in page:
            verdict = _cached_original(vault, ledger, vault / relative)
            if verdict == PRUNED:
                continue  # The pruned row left the candidates, so the next page starts no later.
            remaining += 1
            room += 0 if verdict == KEPT else size
            if room >= shortfall:
                return True
    return False


_PERSONAL_INVENTORY = "personal_eviction.inventory"
_PERSONAL_WAIT = "personal_eviction.wait"
_PERSONAL_CURSOR = "personal_eviction.cursor"
_PERSONAL_EVICTION_MAX_VERSIONS = 16
# One physical object's proof reads at most as many filings as a candidate page.
_PERSONAL_EVICTION_MAX_BINDINGS = _EVICTION_BATCH
# A late route probe neither runs nor hashes on the writer thread.
_PERSONAL_EVICTION_PROBE_SECONDS = 0.05
# A pass yields to the next job after this time or one page. An in-flight
# custody check can take longer and must finish before the worker can yield.
_PERSONAL_EVICTION_SECONDS = 1.0


def _policy_digest(config: dict[str, object]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def _eviction_status_record(conn: sqlite3.Connection, key: str, config: dict[str, object]) -> dict[str, object] | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return None
    try:
        record = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("policy") == _policy_digest(config) else None


def _save_eviction_status(conn: sqlite3.Connection, key: str, config: dict[str, object], **fields: object) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps({"policy": _policy_digest(config), **fields})))


@dataclass
class _PersonalEvictionProgress:
    after: str = ""
    pressure: bool = False
    saw_wait: bool = False

    def restart_scan(self) -> None:
        self.after = ""
        self.saw_wait = False


def _personal_eviction_pass(
    vault: Path, ledger: MediaJobStore, config: dict[str, object], parameters: dict[str, object], *,
    high_water: float, low_water: float,
) -> tuple[int, str | None]:
    from . import media_replicas
    from .cli_ops import OpError
    from .writer_lease import get_manager

    with closing(ledger._connect(readonly=True)) as conn:
        recorded = _eviction_status_record(conn, _PERSONAL_CURSOR, config) or {}
    recorded.pop("policy", None)
    try:
        progress = _PersonalEvictionProgress(**recorded)
    except TypeError:
        progress = _PersonalEvictionProgress()
    starting = vars(progress).copy()
    daily = _DailyEviction(config, high_water, low_water)
    progress.pressure = not daily.has_room(vault, pressure=progress.pressure)
    deadline = time.monotonic() + _PERSONAL_EVICTION_SECONDS
    count = 0
    waiting = None
    try:
        if not progress.pressure:
            waiting = BELOW_TARGET
            return 0, waiting
        page = _personal_candidate_page(ledger, parameters, progress.after)
        for index, row in enumerate(page[:_EVICTION_BATCH]):
            if index and time.monotonic() >= deadline:
                return count, waiting
            if row["logical_rel"] is not None:
                outcome = _evict_one(vault, vault / row["logical_rel"], daily=daily)
                if outcome == POLICY_CHANGED:
                    return count, None
                if outcome == BELOW_TARGET:
                    waiting = BELOW_TARGET
                    return count, waiting
                if outcome == UNAVAILABLE:
                    waiting = "store_unavailable"
                    return count, waiting
                if outcome == BUSY:
                    waiting = "store_busy"
                    return count, waiting
                if outcome == media_replicas.KEY_NOT_EXPORTED or (
                    outcome in (media_replicas.SETTLING, EVICTION_PROOF_LIMIT) and waiting is None
                ):
                    waiting = outcome
                    progress.saw_wait = True
                count += outcome == EVICTED
            progress.after = row["physical_rel"]
        if daily.has_room(vault):
            waiting = BELOW_TARGET
            return count, waiting
        if len(page) <= _EVICTION_BATCH:
            if not progress.saw_wait:
                waiting = "nothing_idle"
            progress.restart_scan()
        return count, waiting
    finally:
        # The owner commands use the same boundary. These local ledger rows do not advance the canonical generation.
        # A busy boundary skips this status row until the next pass, so its refusal is absorbed, not reported.
        from . import mutation_lock

        with (suppress(*_LEDGER_UNREADABLE, ValueError, OpError), mutation_lock.absorbing_busy(),
              get_manager().projection_guard(vault, operation="media_personal_eviction_status"),
              closing(ledger._connect()) as conn, conn):
            conn.execute("BEGIN IMMEDIATE")
            if originals.configuration(vault) == config:
                progress.pressure = progress.pressure and not daily.has_room(vault)
                if starting != vars(progress):
                    _save_eviction_status(conn, _PERSONAL_CURSOR, config, **vars(progress))
                _save_eviction_status(conn, _PERSONAL_WAIT, config, reason=waiting,
                                      observed_at=datetime.now(UTC).isoformat())


def personal_eviction_status(conn: sqlite3.Connection, config: dict[str, object]) -> dict[str, object] | None:
    """Read the last personal inventory and worker decision without opening any original."""
    if not _personal_install():
        return None
    inventory = _eviction_status_record(conn, _PERSONAL_INVENTORY, config) or {}
    wait = _eviction_status_record(conn, _PERSONAL_WAIT, config) or {}
    return {"scope": "bound originals", "unit": "physical_objects", "bytes": "plaintext",
            "local": inventory.get("local"), "evicted": inventory.get("evicted"),
            "observed_at": inventory.get("observed_at"), "inventory_attempted_at": inventory.get("attempted_at"),
            "inventory_state": inventory.get("last_attempt"), "waiting": wait.get("reason"),
            "waiting_observed_at": wait.get("observed_at")}


def _record_inventory_attempt(
    conn: sqlite3.Connection, config: dict[str, object], *, attempted_at: str,
    local: dict[str, int] | None = None, evicted: dict[str, int] | None = None,
) -> None:
    previous = _eviction_status_record(conn, _PERSONAL_INVENTORY, config) or {}
    prior_attempt = previous.get("attempted_at")
    if isinstance(prior_attempt, str) and prior_attempt > attempted_at:
        return
    complete = local is not None and evicted is not None
    _save_eviction_status(conn, _PERSONAL_INVENTORY, config,
                          observed_at=attempted_at if complete else previous.get("observed_at"),
                          attempted_at=attempted_at, last_attempt="complete" if complete else "failed",
                          local=local if complete else previous.get("local"),
                          evicted=evicted if complete else previous.get("evicted"))


def evict_to_target(vault: Path) -> int:
    """Evict under daily pressure. Cloud cells also serve import series demands."""
    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None or not _may_evict(config):
        return 0
    count, _ = _eviction_pass(vault, config, personal=_personal_install())
    return count


def _eviction_pass(vault: Path, config: dict[str, object], *, personal: bool) -> tuple[int, str | None]:
    if config["enabled"] is False:
        # A retained store drains once its evicted originals fit, so a copy that fits stays (D12). Eviction stops
        # without the margin, which leaves the drain's band. An unknown figure evicts as a served cell does:
        # `evict` re-confirms the stored version first.
        try:
            if _retained_gap(vault, config, margin=False) == 0:
                return 0, None
        except MediaStoreError:
            pass
    bounds = media_capacity.bounds_for(config, vault)
    if bounds is None:
        return 0, None
    target = float(config["eviction_target"])
    margin = float(config["eviction_margin"])
    if not 0 < margin < target < 1:
        raise ValueError("media eviction fractions require 0 < margin < target < 1")
    usage = shutil.disk_usage(vault)
    everyday = usage.total * (target - margin) if usage.used > usage.total * target else None
    ledger = MediaJobStore(vault)
    # `size <= :hydration` is the SQL form of `Bounds.hydration_fits`, which `evict` checks again.
    parameters = {"hydration": bounds.hydration_bytes,
                  "idle_before": time.time() - float(config["minimum_idle_seconds"])}
    _refresh_candidates(ledger)
    if personal:
        return _personal_eviction_pass(
            vault, ledger, config, parameters,
            high_water=target, low_water=target - margin,
        )
    demand = _live_demand(ledger)

    def needed(used: int) -> tuple[bool, bool]:
        return (everyday is not None and used > everyday,
                demand is not None and demand[1] - used < demand[0])

    used = shutil.disk_usage(vault).used
    above_target, short = needed(used)
    if not above_target and short:
        assert demand is not None
        if not _room_covers(vault, ledger, parameters, demand[0] - (demand[1] - used)):
            return 0, None  # A demand that the evictable originals cannot cover yet evicts nothing.
    count = kept = 0
    while any(needed(shutil.disk_usage(vault).used)) and (page := _candidates(ledger, parameters, kept)):
        for relative, _size in page:
            if not any(needed(shutil.disk_usage(vault).used)):
                break
            outcome = _evict_one(vault, vault / relative)
            if outcome == UNAVAILABLE:
                return count, "store_unavailable"
            if outcome == BUSY:
                # Each later candidate would wait for the same store or boundary: the next pass
                # asks again.
                return count, None
            # An evicted or pruned row leaves the candidates; a kept one stays, so the next page starts after it.
            count += outcome == EVICTED
            kept += outcome not in (EVICTED, PRUNED)
    return count, None


# -- the drain of a retained store (D12) --
#
# A cell whose store is turned off keeps its key until a report shows no evicted original. The cell hydrates
# its evicted originals back while they fit, counts the rest from the companions, and records the count. Only
# the cell can make that count final: the backup does not stop it, so a zero latches eviction off.

# A complete census repeats at most this often; an unknown one waits as long. One hour keeps a walk of the
# Knowledge Base rare while a restore that brings back evicted originals is seen within the hour.
CENSUS_INTERVAL_SECONDS = 3600
# The drain wire protocol counts physical objects, independent of logical filing count.
DRAIN_UNIT = "physical_objects"


# What a drain pass yielded to; the codes of a busy slot or store are the hydration's own.
WAIT_OWNER_READ, WAIT_PENDING_JOB = "OWNER_READ_RECENT", "PENDING_JOB"


@dataclass(frozen=True, slots=True)
class DrainRecord:
    """The last complete census, and whether it latched; `evicted` is None until one completes."""

    evicted: int | None
    evicted_bytes: int | None
    captured_at: str | None
    latched: bool
    attempted_at: str
    attempt_failed: bool  # The latest attempt could not count, so the figures above are from an earlier census.
    waiting: str | None
    blocked_by: tuple[str, ...]  # The originals whose companion stopped the latest attempt, vault-relative.
    unit: str = DRAIN_UNIT


def drain_record(vault: Path) -> DrainRecord | None:
    """The census record, or None when no census was tried; raises `MediaStoreError` when the ledger cannot be read."""
    ledger = MediaJobStore(Path(vault).resolve(), create=False)
    if not ledger.path.exists():
        return None
    try:
        with closing(ledger._connect(readonly=True)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'media_drain'").fetchone() is None:
                return None  # A ledger from before the record has no table.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(media_drain)")}
            proof = "proof_version" if "proof_version" in columns else "0 AS proof_version"
            row = conn.execute("SELECT evicted, bytes, captured_at, latched, attempted_at, attempt_failed, waiting, "
                               f"blocked_by, {proof} FROM media_drain").fetchone()
    except _LEDGER_UNREADABLE as error:
        raise MediaStoreError("MEDIA_LEDGER_UNREADABLE", f"the media ledger cannot be read: {error}") from error
    if row is None:
        return None
    if row["proof_version"] != 1:
        return DrainRecord(None, None, None, False, row["attempted_at"], True, row["waiting"],
                           tuple(json.loads(row["blocked_by"] or "[]")))
    return DrainRecord(row["evicted"], row["bytes"], row["captured_at"], bool(row["latched"]), row["attempted_at"],
                       bool(row["attempt_failed"]), row["waiting"], tuple(json.loads(row["blocked_by"] or "[]")))


def evicted_above_capacity(vault: Path, config: dict[str, object]) -> int:
    """How many evicted originals the hydration capacity can never hold; they keep the store from draining."""
    bounds = media_capacity.bounds_for(config, Path(vault).resolve())
    if bounds is None:
        raise MediaStoreError("MEDIA_CAPACITY_UNKNOWN", "the media store policy sets no hydration capacity")
    ledger = MediaJobStore(Path(vault).resolve(), create=False)
    if not ledger.path.exists():
        raise MediaStoreError("MEDIA_LEDGER_UNREADABLE", "the media ledger does not exist")
    try:
        with closing(ledger._connect(readonly=True)) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM media_cache WHERE evicted = 1 AND size > ?",
                                    (bounds.hydration_bytes,)).fetchone()[0])
    except _LEDGER_UNREADABLE as error:
        raise MediaStoreError("MEDIA_LEDGER_UNREADABLE", f"the media ledger cannot be read: {error}") from error


def _on_volume(vault: Path, relative: str) -> bool:
    """Whether the file is on the volume; raises when the volume cannot say."""
    try:
        reserved_paths.inspect_generic_file(vault, relative)
    except reserved_paths.ReservedPathLeafError as error:
        if error.code != "MISSING":
            raise
        return False
    return True


def _pool_holds(vault: Path, binary: originals.BinaryBinding) -> bool:
    """Whether the shared pool holds this binding's bytes locally; an export ships that object as a canonical member."""
    from . import original_pool

    return original_pool.enabled(vault) and original_pool.locally_present(vault, binary.sha256, binary.size)


def _physical_drain_census(vault: Path, conn: sqlite3.Connection) -> None:
    """Stream unique objects into temporary ledger state; absence without own recovery is unknown."""
    from contextlib import nullcontext

    from . import original_pool
    from .vault_store import mode, read

    conn.execute("CREATE TEMP TABLE drain_census (object_key TEXT PRIMARY KEY, sha256 TEXT, size INTEGER, "
                 "binary_rel TEXT, reference TEXT, present INTEGER NOT NULL DEFAULT 0)")
    if conn.execute("SELECT 1 FROM media_bindings GROUP BY object_key "
                    "HAVING MIN(sha256) != MAX(sha256) OR MIN(size) != MAX(size) LIMIT 1").fetchone():
        raise ValueError("one physical original has inconsistent bindings")
    conn.execute(
        "INSERT INTO drain_census(object_key, sha256, size, binary_rel, reference) "
        "SELECT object_key, MIN(sha256), MIN(size), "
        "COALESCE(MIN(CASE WHEN reference IS NOT NULL THEN binary_rel END), MIN(binary_rel)), MIN(reference) "
        "FROM media_bindings GROUP BY object_key")
    conn.commit()
    blocked = []
    with (read.snapshot(vault, join_request=False) if mode.store_mode_for_reads(vault) else nullcontext(None)) as view:
        cursor = conn.execute("SELECT * FROM drain_census ORDER BY object_key")
        while batch := cursor.fetchmany(256):
            owned = read.owned_as_pages(vault, (row["object_key"] for row in batch)) if view else {}
            stored = view.held_pages(key for key, serves in owned.items() if serves) if view else ()
            updates = []
            for row in batch:
                key = row["object_key"]
                if owned.get(key):
                    present = key in stored
                elif original_pool.is_canonical_member(key):
                    if key != original_pool.object_path(vault, row["sha256"]).relative_to(vault).as_posix():
                        raise ValueError("physical original has an invalid pool identity")
                    present = original_pool.locally_present(vault, row["sha256"], row["size"])
                else:
                    present = _on_volume(vault, key)
                updates.append((int(present), key))
                if not present and row["reference"] is None:
                    blocked.append(row["binary_rel"])
                    blocked.sort()
                    del blocked[media_allowance.BLOCKERS_SHOWN:]
            conn.executemany("UPDATE drain_census SET present = ? WHERE object_key = ?", updates)
    if blocked:
        raise _CompanionBlocks(sorted(blocked))
    # Preserve old-ledger witnesses when no complete binding projection had acquired them.
    for row in conn.execute("SELECT binary_rel FROM media_cache c WHERE NOT EXISTS "
                            "(SELECT 1 FROM drain_census d WHERE d.object_key = c.binary_rel) ORDER BY binary_rel"):
        relative = row[0]
        if original_pool.is_canonical_member(relative) or _on_volume(vault, relative):
            continue
        try:
            _require_released(vault, relative)
        except ValueError:
            blocked.append(relative)
            if len(blocked) == media_allowance.BLOCKERS_SHOWN:
                break
    if blocked:
        raise _CompanionBlocks(blocked)
    conn.commit()



def _require_released(vault: Path, relative: str) -> None:
    """Raise unless the companion of an original with no bytes is gone or holds no reference to it.

    The walk reads a file without a front matter fence as a note, and an undecodable one not at all. A
    companion that is empty, plain text, undecodable, cut off or bound to another path may be the one that held
    this original's reference, so it is no release: the census cannot call the original gone and stays unknown.
    """
    found = originals.bound_companion(vault, relative)  # Raises on bytes that are not a front matter.
    if found is None:
        if any(True for _ in originals.companion_candidates(vault, relative)):
            raise ValueError("a companion that binds no path stands where an evicted original's belongs")
    elif "stored_object" in found[1]:
        raise ValueError("a companion holds a reference that the census did not count")


class _CompanionBlocks(ValueError):
    """Bound originals without local bytes or proven release; paths stay off worker log messages."""

    def __init__(self, blocked: list[str]) -> None:
        super().__init__("an original has no local bytes or proven release")
        self.blocked = blocked






def drain_census(vault: Path, *, inventory_only: bool = False) -> tuple[int, int]:
    """Count physical objects off-volume; publish only an unchanged complete binding frontier."""
    from . import media_bindings
    from .cli_ops import OpError

    vault = Path(vault).resolve()
    config, binding = originals.configuration(vault), originals.binding_for(vault)
    if config is None or binding is None:
        raise MediaStoreError("MEDIA_CENSUS_UNKNOWN", "the vault has no media store")
    ledger = MediaJobStore(vault)
    now = datetime.now(UTC)
    personal = _personal_install()
    try:
        if not media_bindings.rebuild(vault):
            blocked = []
            with closing(ledger._connect(readonly=True)) as conn:
                for row in conn.execute("SELECT c.binary_rel, b.binary_rel AS filing FROM media_cache c "
                                        "LEFT JOIN media_bindings b ON b.object_key = c.binary_rel ORDER BY c.binary_rel"):
                    relative = row["filing"] or row["binary_rel"]
                    if reserved_paths.classify_logical(relative).blocked or _on_volume(vault, relative):
                        continue
                    try:
                        _require_released(vault, relative)
                    except ValueError:
                        blocked.append(relative)
                        if len(blocked) == media_allowance.BLOCKERS_SHOWN:
                            break
            if blocked:
                raise _CompanionBlocks(blocked)
            raise ValueError("the canonical binding inventory could not complete")
        with closing(ledger._connect()) as conn:
            with _guard(vault, "media_drain_census_start"):
                before = media_bindings.checkpoint(conn)
                if not media_bindings.current(vault, conn):
                    raise ValueError("the canonical binding inventory is incomplete")
                policy = media_bindings.policy_key(vault)
                config = originals.configuration(vault)
                if config is None:
                    raise ValueError("the media store policy disappeared")
                data_version = conn.execute("PRAGMA data_version").fetchone()[0] if personal else None
            _physical_drain_census(vault, conn)
            count, size = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) "
                                       "FROM drain_census WHERE present = 0").fetchone()
            if personal:
                local_count, local_size = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) "
                                                       "FROM drain_census WHERE present = 1").fetchone()
            with _guard(vault, "media_drain_census_publish"):
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if (media_bindings.checkpoint(conn) != before or not media_bindings.current(vault, conn)
                            or media_bindings.policy_key(vault) != policy):
                        raise ValueError("the canonical bindings changed during the census")
                    if personal:
                        if (conn.execute("PRAGMA data_version").fetchone()[0] != data_version
                                or originals.configuration(vault) != config):
                            raise ValueError("media custody changed during the inventory")
                        _record_inventory_attempt(conn, config, attempted_at=now.isoformat(),
                                                  local={"count": local_count, "bytes": local_size},
                                                  evicted={"count": count, "bytes": size})
                    if not inventory_only:
                        conn.execute("UPDATE media_cache SET evicted = 0 WHERE evicted != 0")
                        conn.execute(
                            "INSERT INTO media_cache(binary_rel, reference, sha256, identity, last_read, size, evicted) "
                            "SELECT object_key, reference, sha256, '', ?, size, 1 FROM drain_census WHERE present = 0 "
                            "ON CONFLICT(binary_rel) DO UPDATE SET reference = excluded.reference, "
                            "sha256 = excluded.sha256, size = excluded.size, evicted = 1", (time.time(),))
                        conn.execute(
                            "INSERT INTO media_drain(singleton, evicted, bytes, captured_at, latched, attempted_at, proof_version) "
                            "VALUES (1, :count, :size, :at, :latched, :at, 1) ON CONFLICT(singleton) DO UPDATE SET "
                            "evicted = :count, bytes = :size, captured_at = :at, latched = :latched, attempted_at = :at, "
                            "attempt_failed = 0, blocked_by = NULL, proof_version = 1",
                            {"count": count, "size": size, "at": now.isoformat(timespec="seconds"),
                             "latched": int(count == 0 and config.get("enabled") is False)})
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
    except (*media_allowance.UNREADABLE, sqlite3.Error) as error:
        if personal and config is not None:
            with suppress(*_LEDGER_UNREADABLE, ValueError, OpError), _guard(vault, "media_inventory_failure"):
                if originals.configuration(vault) == config:
                    with closing(ledger._connect()) as conn, conn:
                        conn.execute("BEGIN IMMEDIATE")
                        _record_inventory_attempt(conn, config, attempted_at=now.isoformat())
        if not inventory_only:
            with suppress(*_LEDGER_UNREADABLE), closing(ledger._connect()) as conn, conn:
                conn.execute("INSERT INTO media_drain(singleton, attempted_at, attempt_failed, blocked_by) VALUES (1, ?, 1, ?) "
                             "ON CONFLICT(singleton) DO UPDATE SET attempted_at = excluded.attempted_at, attempt_failed = 1, "
                             "blocked_by = excluded.blocked_by",
                             (now.isoformat(timespec="seconds"),
                              json.dumps(error.blocked) if isinstance(error, _CompanionBlocks) else None))
        raise MediaStoreError("MEDIA_CENSUS_UNKNOWN", f"the companions or the ledger cannot be read ({type(error).__name__})") from error
    # A committed latch precedes its externally consumed three-token record.
    if not inventory_only:
        media_drain_record.write(vault, count, size, now)
    return count, size



def _census_due(vault: Path) -> bool:
    record = drain_record(vault)
    if record is None:
        return True
    try:
        if (datetime.now(UTC) - datetime.fromisoformat(record.attempted_at)).total_seconds() >= CENSUS_INTERVAL_SECONDS:
            return True
    except ValueError:
        return True
    if record.latched or record.attempt_failed:
        return False  # Drained, or the last census was unknown and waits out its hour.
    # The last census completed and the ledger shows no evicted original: the zero may be here now.
    ledger = MediaJobStore(Path(vault).resolve(), create=False)
    try:
        with closing(ledger._connect(readonly=True)) as conn:
            return conn.execute("SELECT 1 FROM media_cache WHERE evicted = 1 LIMIT 1").fetchone() is None
    except _LEDGER_UNREADABLE:
        return False


def hydrate_evicted(vault: Path, path: Path) -> bool:
    """Hydrate one evicted original into its folder and record no owner read; False when it is not evicted.

    It pins the original and takes the hydration slot as an owner's read does, so it raises `STORE_BUSY`
    while a read or a job holds the capacity.
    """
    with _pin(vault, path) as (ledger, token):
        snapshot = original_state(vault, path)
        if snapshot.state != "evicted":
            return False
        _hydrate(vault, snapshot, ledger, token)
    return True


def drain_step(vault: Path, claimable: Callable[[], object | None] | None = None) -> int:
    """Hydrate evicted originals back, smallest first, while they fit below the growth threshold (D12).

    It runs only in the draining mode: offload off and eviction on. It starts only when the evicted bytes
    fit in the room less the eviction margin, and then eviction, which runs only while they do not fit, never
    undoes it. It leaves the volume to owner reads and jobs: it waits out the idle time, yields to a pending job
    and ends on a busy hydration slot. An original above the hydration capacity stays evicted and counted.
    `claimable` answers the job that the worker would claim now; the default is the oldest pending job.
    The record keeps what the pass yielded to, for the status. Returns the number hydrated; raises
    `MediaStoreError` when the ledger or the volume cannot say.
    """
    vault = Path(vault).resolve()
    hydrated, waiting = _drain_pass(vault, claimable)
    with closing(MediaJobStore(vault)._connect()) as conn, conn:
        conn.execute("UPDATE media_drain SET waiting = ? WHERE singleton = 1 AND waiting IS NOT ?", (waiting, waiting))
    return hydrated


def _drain_pass(vault: Path, claimable: Callable[[], object | None] | None) -> tuple[int, str | None]:
    """One pass of `drain_step`: the number hydrated, and what the pass yielded to, if anything."""
    config = originals.configuration(vault)
    if config is None or config.get("enabled") is not False:
        return 0, None
    personal_drain = originals.uses_local_configuration() and config.get("eviction_enabled") is False
    if not personal_drain and config.get("eviction_enabled") is not True:
        return 0, None
    threshold, bounds = media_capacity.growth_threshold(config), media_capacity.bounds_for(config, vault)
    try:
        margin, idle = float(config["eviction_margin"]), float(config["minimum_idle_seconds"])
    except (KeyError, TypeError, ValueError):
        return 0, None
    record = drain_record(vault)
    if threshold is None or bounds is None or (record is not None and record.latched):
        return 0, None
    if not personal_drain and retained_shortfall(vault, config):
        return 0, None  # Inside the margin band neither this nor eviction moves a byte.
    ledger = MediaJobStore(vault)
    claimable = claimable or ledger.next_pending
    hydrated = skipped = 0
    while True:
        with closing(ledger._connect(readonly=True)) as conn:
            owner_read = conn.execute("SELECT MAX(owner_read) FROM media_cache").fetchone()[0]
            usage = shutil.disk_usage(vault)
            # What one more original may add: below the threshold less the margin, within the hydration capacity.
            fits = min(bounds.hydration_bytes, int(threshold * usage.total) - int(margin * usage.total) - usage.used)
            row = conn.execute(
                "SELECT COALESCE((SELECT b.binary_rel FROM media_bindings b WHERE b.object_key = c.binary_rel "
                "AND b.reference IS NOT NULL ORDER BY b.binary_rel LIMIT 1), c.binary_rel) AS binary_rel "
                "FROM media_cache c WHERE c.evicted = 1 AND c.size IS NOT NULL AND c.size <= ? "
                "ORDER BY c.size, c.binary_rel LIMIT 1 OFFSET ?", (fits, skipped)).fetchone()
        if row is None:
            return hydrated, None
        if owner_read is not None and time.time() - owner_read < idle:
            return hydrated, WAIT_OWNER_READ
        if claimable() is not None:
            return hydrated, WAIT_PENDING_JOB
        if reserved_paths.classify_logical(row["binary_rel"]).blocked:
            skipped += 1  # An orphan physical row supplies no filing authority for hydration.
            continue
        try:
            done = hydrate_evicted(vault, vault / row["binary_rel"])
        except MediaStoreError as error:
            if error.code in RETRYABLE_HYDRATION_CODES:
                return hydrated, error.code  # An outage or a busy slot ends the pass; the next tick asks again.
            skipped += 1  # An original that fails its check stays counted, and the pass goes on to the next.
            continue
        hydrated += done
        skipped += not done


def clear_served_drain(vault: Path) -> None:
    """Drop the record and the latch of a store that is served: a later turn-off must count afresh (D12).

    The runtime start and each drain tick call it, so a zero from an earlier turn-off cannot outlive the turn-on.
    """
    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None or config.get("enabled") is not True:
        return
    if drain_record(vault) is not None:
        with closing(MediaJobStore(vault)._connect()) as conn, conn:
            conn.execute("DELETE FROM media_drain")
    media_drain_record.remove(vault)


def drain_tick(vault: Path, claimable: Callable[[], object | None] | None = None) -> None:
    """One tick of a retained store's drain, after eviction: hydrate what fits, then count when a census is due.

    A served policy clears the record and the latch. A store that is off counts, so that a restore that brings
    evicted originals back is seen and cellctl keys the cell again.
    """
    vault = Path(vault).resolve()
    clear_served_drain(vault)
    config = originals.configuration(vault)
    if config is None:
        return
    if config.get("enabled") is not False:
        if originals.uses_local_configuration() and _may_evict(config) and _personal_inventory_due(vault, config):
            drain_census(vault, inventory_only=True)
        return
    try:
        drain_step(vault, claimable)
    except Exception:  # noqa: BLE001 - a failed step hydrates nothing; the census below is what repairs a missing size
        log.warning("media drain: hydration step skipped", exc_info=True)
    if _census_due(vault):
        drain_census(vault)


def _personal_inventory_due(vault: Path, config: dict[str, object]) -> bool:
    try:
        with closing(MediaJobStore(vault, create=False)._connect(readonly=True)) as conn:
            record = _eviction_status_record(conn, _PERSONAL_INVENTORY, config)
        if record is not None:
            attempted = record.get("attempted_at") or record["observed_at"]
            return (datetime.now(UTC) - datetime.fromisoformat(attempted)).total_seconds() >= CENSUS_INTERVAL_SECONDS
    except (*_LEDGER_UNREADABLE, KeyError, TypeError, ValueError):
        pass
    return True
