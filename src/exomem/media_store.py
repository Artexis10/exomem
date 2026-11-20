"""Encrypted, version-pinned originals; the volume is a verified local cache.

A store is selected only by explicit configuration: the media edge for Cloud cells,
and a filesystem adapter for a local disk or a mounted NAS. No store is selected by default.
Jobs share the media ledger; protection records outlive those jobs.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import chain
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO, Protocol

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
    from .vault import PlannedWrite

log = logging.getLogger(__name__)


class MediaStoreError(RuntimeError):
    def __init__(self, code: str, reason: str):
        self.code = code
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


def _digest(stream: BinaryIO, *, deadline: float | None = None) -> ObjectHead:
    """The size and SHA-256 of a stream; a request's `deadline` stops the pass as the ordinary hold's hash does."""
    digest = hashlib.sha256()
    size = 0
    while True:
        reserved_paths._copy_deadline(deadline)
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return ObjectHead(size, digest.hexdigest())
        size += len(chunk)
        digest.update(chunk)


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


#: The adapter name of a local disk or a mounted NAS, and its earlier name, which stays accepted.
FILESYSTEM_ADAPTER = "filesystem"
FILESYSTEM_ADAPTER_ALIAS = "filesystem-test"
#: The file in a filesystem store's root that names the store that the root is. Only `declare_store` writes it.
STORE_MARKER = ".exomem-store"
_MARKER_SCHEMA = 1
_MARKER_MAX_BYTES = 4096


def _read_marker(root: Path) -> dict[str, object] | None:
    """The marker's content; None when the root, the marker or its content is absent, unreadable or not a marker."""
    try:
        descriptor = os.open(root / STORE_MARKER, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    with os.fdopen(descriptor, "rb") as marker:
        if not stat.S_ISREG(os.fstat(marker.fileno()).st_mode):
            return None
        raw = marker.read(_MARKER_MAX_BYTES + 1)
    try:
        value = json.loads(raw) if len(raw) <= _MARKER_MAX_BYTES else None
    except ValueError:
        return None
    if isinstance(value, dict) and value.get("schema") == _MARKER_SCHEMA and isinstance(value.get("store"), str):
        return value
    return None


def read_store_marker(root: Path) -> str | None:
    """The store name that `root`'s marker declares; None when the root, the marker or its content is absent or unreadable."""
    found = _read_marker(root)
    return None if found is None else str(found["store"])


def read_store_id(root: Path) -> str | None:
    """The random id that `root`'s marker carries; None when there is no marker, or an older one that has no id."""
    found = _read_marker(root)
    return found["id"] if found is not None and isinstance(found.get("id"), str) else None


def write_store_marker(root: Path, name: str) -> str:
    """Create the marker that names `root` as store `name`, with a new random id, and return the id; refuses to replace one.

    `declare_store` is the owner's door to it.
    """
    store_id = uuid.uuid4().hex
    descriptor = os.open(root / STORE_MARKER, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                         | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(descriptor, "wb") as marker:
        marker.write(json.dumps({"schema": _MARKER_SCHEMA, "store": name, "id": store_id},
                                separators=(",", ":")).encode("utf-8"))
        marker.flush()
        os.fsync(marker.fileno())
    held_fs.flush_directory_path(root).require()
    return store_id


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
        found = _read_marker(self.root)
        if found is None or found["store"] != self.name or (self.store_id is not None and found.get("id") != self.store_id):
            # Unavailable is never proof that a version is gone: the caller keeps what it recorded.
            raise MediaStoreError("MEDIA_STORE_UNAVAILABLE",
                                  "the store root is missing, or its marker does not name this store")

    def _path(self, name: str, version: str) -> str:
        if not originals.is_sha256(name) or not version or Path(version).name != version or version in {".", ".."}:
            raise ValueError("invalid object locator")
        return f"{name}/{version}"

    def upload(self, name: str, stream: BinaryIO, *, size: int, sha256: str) -> str:
        version = uuid.uuid4().hex
        self._path(name, version)
        if not originals.is_sha256(sha256) or size < 0:
            raise ValueError("invalid upload declaration")
        if self.max_object_bytes is not None and size > self.max_object_bytes:
            raise ObjectTooLarge(size, self.max_object_bytes)
        self.require_identity()
        with held_fs.acquire(self.root).require() as filesystem:
            with filesystem.parent(name, create=True, access="mutate").require() as parent:
                held_fs.publish_bytes(filesystem, parent, version,
                                      held_fs.Chunks(lambda: _declared_chunks(stream, size, sha256))).require()
                filesystem.flush_directory(parent).require()
        return version

    @contextmanager
    def open(self, name: str, version: str):
        relative = self._path(name, version)
        self.require_identity()
        with held_fs.acquire(self.root).require() as filesystem:
            with filesystem.parent(name).require() as parent:
                with filesystem.file(parent, Path(relative).name).require() as file:
                    with os.fdopen(os.dup(file.descriptor), "rb") as stream:
                        yield stream

    def head(self, name: str, version: str) -> ObjectHead | None:
        self._path(name, version)
        self.require_identity()
        with held_fs.acquire(self.root).require() as filesystem:
            parent_result = filesystem.parent(name)
            if parent_result.error is not None and parent_result.error.code == "MISSING":
                return None
            with parent_result.require() as parent:
                found = filesystem.file(parent, version)
                if found.error is not None and found.error.code == "MISSING":
                    return None
                with found.require() as file:
                    with os.fdopen(os.dup(file.descriptor), "rb") as stream:
                        return _digest(stream)


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
        if adapter not in (FILESYSTEM_ADAPTER, FILESYSTEM_ADAPTER_ALIAS) or not isinstance(connection.get("root"), str):
            raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the media store adapter is unavailable")
        bound = connection.get("max_object_bytes")
        return FilesystemStore(Path(str(connection["root"])), name=name,
                               max_object_bytes=None if bound is None else int(bound))
    except (TypeError, ValueError) as error:
        # A connection field of the wrong type or form makes this one store unavailable, never the caller's whole pass.
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", f"the store's connection fields are invalid: {error}") from error


def store_for(vault: Path, name: str | None = None) -> Store:
    """The store that serves eviction, or the declared store `name`; a declared store with an incomplete descriptor is unavailable."""
    config = _settings(vault)
    entry = originals.store_entry(config, name)
    if entry is None:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "no media store of that name is configured")
    if not entry.serves_eviction and not entry.available:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the store's descriptor is incomplete")
    if (refusal := connection_refusal(entry)) is not None:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", refusal)
    backend = _adapter(entry.connection, entry.name)
    if isinstance(backend, FilesystemStore):
        from . import media_replicas

        backend.store_id = media_replicas.declared_store_id(vault, entry.name)
    return backend


def connection_refusal(entry: originals.StoreEntry) -> str | None:
    """Why a store's entry cannot name a connection of its own, or None.

    The media edge is one connection for the whole process (its address and credential come from the environment),
    so a declared further store of that adapter would silently be the store that serves eviction again: a replica in
    the same place, counted as a second failure domain.
    """
    if not entry.serves_eviction and entry.connection.get("adapter") == "media-edge":
        return "a media-edge store has no connection of its own: only the store that serves eviction uses the edge"
    return None


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
    if os.stat(folder).st_dev == os.stat(Path(vault)).st_dev:
        raise MediaStoreError("MEDIA_STORE_DECLARATION_REFUSED",
                              "the store's root is on the vault's own device, so it would not survive that device's loss")
    from . import media_replicas

    declared = read_store_marker(folder)
    if declared is not None and declared != name:
        raise MediaStoreError("MEDIA_STORE_DECLARATION_REFUSED", "the store's root already names another store")
    recorded = media_replicas.declared_store_id(vault, name)
    if declared == name:
        # The same store again: its marker's id is recorded (also after a lost ledger), unless another id is on record.
        found = read_store_id(folder)
        if recorded is not None and found != recorded:
            raise MediaStoreError("MEDIA_STORE_DECLARATION_REFUSED",
                                  "the store's root carries a marker other than the one recorded for this store; if this is "
                                  "a new disk on purpose, remove its marker file and declare the store again")
        if found is not None and found != recorded:
            media_replicas.record_store_id(vault, name, found)
        return folder
    try:
        store_id = write_store_marker(folder, name)
    except FileExistsError:
        raise MediaStoreError("MEDIA_STORE_DECLARATION_REFUSED", "the store's root holds a marker that cannot be read") from None
    media_replicas.record_store_id(vault, name, store_id)
    return folder


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


def cached_sha256(vault: Path, path: Path, version: held_fs.FileVersion) -> str | None:
    """The verified SHA-256 of a file at this exact version when the cache identity records it, else None.

    A caller that holds the file passes the version taken from its handle, so a match
    reads no byte (D3). None means the file has no record or no longer matches it.
    """
    vault = Path(vault).resolve()
    selected = original_state(vault, path)
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
def _pin(vault: Path, path: Path, *, read: bool = False, object_key: str | None = None):
    """Keep an original on the volume; `read` marks an owner's playback, download or viewing (D3, M4)."""
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
        conn = store._connect()
        try:
            with conn:
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
        finally:
            conn.close()


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


def _enqueue_offload(vault: Path, path: Path, *, usage: media_allowance.ReferenceUsage | None = None) -> int:
    config = originals.configuration(vault)
    if config is None:
        return 0
    snapshot = original_state(vault, path)
    if snapshot.state != "local" or snapshot.binary_sha256 is None or not snapshot.offloadable:
        return 0
    store = MediaJobStore(vault)
    if _refused(store, snapshot, config, usage=usage) or _resident(vault, snapshot, config):
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
    from . import media_bindings, original_pool
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
        return sum(_enqueue_offload(vault, path, usage=usage) for path in _offload_candidates(vault))


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


def _publishing(vault: Path, operation: str, unit: Callable[[], None]) -> None:
    """Run a unit that publishes a reference (``_publish_reference``) under the vault boundary.

    Its sidecar commit runs on the store thread in a store-mode vault, so the unit runs there
    (``vault_store.media.holding_boundary``). A boundary that stays busy is ordinary contention,
    reported as ``STORE_BUSY_CODE``, which returns an offload job to the queue without spending an
    attempt.
    """
    from .vault_store import media as store_media
    from .vault_store import projector

    try:
        store_media.holding_boundary(vault, operation, unit, lambda: _guard(vault, operation))
    except projector.BoundaryBusy as busy:
        raise MediaStoreError(STORE_BUSY_CODE, "the vault boundary is busy") from busy


def _binding_of(vault: Path, snapshot: OriginalSnapshot) -> originals.BinaryBinding:
    assert snapshot.binary_sha256 is not None and snapshot.binary_size is not None
    return originals.BinaryBinding(snapshot.path.relative_to(vault).as_posix(),
                                   snapshot.binary_sha256, snapshot.binary_size)


class OffloadBatch:
    """One owner census for a run of offloads, kept across guard releases (D10).

    The batch never holds the mutation boundary, so other writes run between two members. It rests
    on offload being the only writer that binds a version the census has not seen while the runtime
    serves; design.md D10 lists every writer of a stored reference (task 5.4.9). `charge` adds each
    version that a member publishes. `end` drops the census so that the next member reads it again,
    and `failed` does so only for a member that reached `charge`: a refused member bound nothing, so
    the next one reuses the census. A stale census can wrongly refuse a member, which the next scan
    releases, because refusals are read again from a fresh census.
    """

    def __init__(self, vault: Path) -> None:
        self._vault = Path(vault).resolve()
        self._usage: media_allowance.ReferenceUsage | None = None
        self._charged = False

    def usage(self, ledger: MediaJobStore) -> media_allowance.ReferenceUsage:
        if self._usage is None:
            self._usage = media_allowance.reference_usage(self._vault)
            _record_census(ledger, self._usage)
        return self._usage

    def begin(self) -> None:
        self._charged = False

    def charge(self, ledger: MediaJobStore, snapshot: OriginalSnapshot, reference: StoredObject) -> None:
        self._charged = True
        self._usage = self.usage(ledger).charged(_binding_of(self._vault, snapshot), reference)
        _record_census(ledger, self._usage)

    def failed(self) -> None:
        """A member raised: drop the census when that member may have half-published, which the census cannot know."""
        if self._charged:
            self.end()

    def end(self) -> None:
        self._usage = None


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


def _offload_admission(
    vault: Path, snapshot: OriginalSnapshot, *, reference: StoredObject | None = None,
    select_reuse: bool = True, usage: media_allowance.ReferenceUsage | None = None,
) -> tuple[media_allowance.AllowanceDecision, OriginalSnapshot | None]:
    """Select current live reuse and its quota projection under one read boundary."""
    from .writer_lease import get_manager

    binary = _binding_of(vault, snapshot)
    with get_manager().consistency_guard(vault, operation="media_offload_admission", holder_kind="background"):
        config = originals.configuration(vault) or {}
        # A caller shares one census while its enclosing consistency guard holds, or as an `OffloadBatch`.
        if usage is None:
            usage = media_allowance.reference_usage(vault)
        reuse = None
        if select_reuse and usage.versions is not None:
            for key, path in usage.live_paths.items():
                candidate_binary, candidate_reference = usage.versions[key]
                if (candidate_binary.sha256 != binary.sha256 or candidate_binary.size != binary.size
                        or candidate_reference.key_version != str(config.get("key_version"))):
                    continue
                try:
                    candidate = original_state(vault, path)
                    if (candidate.reference != candidate_reference or candidate.binary_sha256 != binary.sha256
                            or candidate.binary_size != binary.size):
                        raise ValueError("the live reuse binding changed")
                except (OSError, ValueError, held_fs.HeldFsError, reserved_paths.ReservedPathLeafError):
                    usage = media_allowance.ReferenceUsage(None, usage.observed_at)
                    break
                reuse, reference = candidate, candidate_reference
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


def _verified_head(store: Store, reference: StoredObject) -> ObjectHead | None:
    """The version's HEAD, None when it is definitively absent. A different ciphertext raises."""
    with _transport("head"):
        head = store.head(reference.object, reference.version)
    if head is not None and (head.size, head.sha256) != (reference.ciphertext_size, reference.ciphertext_sha256):
        raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the pinned ciphertext differs")
    return head


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
             replica_store: str = "") -> None:
    # One record per object covers every retried attempt for this original.
    relative = store._relative(snapshot.path)
    _durable(store, "INSERT INTO media_protections(token, binary_rel, object, created_at, uploadable, store) "
                    "SELECT ?, ?, ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM media_protections "
                    "WHERE binary_rel = ? AND object = ?)",
             (uuid.uuid4().hex, relative, name, time.time(), int(uploadable), replica_store, relative, name))


def _seal(store: MediaJobStore, snapshot: OriginalSnapshot, name: str) -> None:
    """Before a sidecar can carry `name`, take it out of upload reuse; the record keeps protecting it."""
    _durable(store, "UPDATE media_protections SET uploadable = 0 WHERE binary_rel = ? AND object = ?",
             (store._relative(snapshot.path), name))


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
    usage: media_allowance.ReferenceUsage | None = None,
) -> str | None:
    conn = store._connect(readonly=True)
    try:
        row = conn.execute("SELECT sha256, code, ciphertext_size FROM media_offload_refusals "
                           "WHERE binary_rel = ? AND sha256 = ?",
                           (store._relative(snapshot.path), snapshot.binary_sha256)).fetchone()
    finally:
        conn.close()
    return _refusal_code(store.vault_root, row, snapshot, config, usage=usage) if row is not None else None


def _refusal_code(
    vault: Path, row, snapshot: OriginalSnapshot, config: dict[str, object], *,
    usage: media_allowance.ReferenceUsage | None = None,
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


def _no_census() -> None:
    return None


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


def _upload_encrypted(ledger: MediaJobStore, token: str, config: dict[str, object], backend: Store, name: str,
                      plaintext: BinaryIO, plaintext_size: int) -> tuple[ObjectHead, str]:
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
            with _transport("upload"):
                version = backend.upload(name, ciphertext, size=head.size, sha256=head.sha256)
            return head, version


def offload(vault: Path, path: Path, *, batch: OffloadBatch | None = None,
            job: MediaJob | None = None) -> StoredObject | None:
    """Protect, encrypt, confirm and durably publish a bound original.

    None means that the original stays local: notes and datasets are never
    offloaded. Quota refusals raise so that the worker retains retryable demand (D10).
    A `batch` shares its census with this member's checks; without one, each check reads its own.
    """
    if batch is not None:
        batch.begin()
    try:
        return _offload_member(Path(vault).resolve(), path, batch, job=job)
    except BaseException:
        if batch is not None:
            batch.failed()
        raise


def _offload_member(vault: Path, path: Path, batch: OffloadBatch | None, *,
                    job: MediaJob | None = None) -> StoredObject | None:
    config = _settings(vault)
    if config.get("enabled") is not True:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "offload is disabled")
    backend = store_for(vault)
    ledger = MediaJobStore(vault)
    census = (lambda: batch.usage(ledger)) if batch is not None else _no_census
    with _pin(vault, path) as (_ledger, token), _offload_input(vault, path, ledger) as snapshot:
        if snapshot.binary_sha256 is None or snapshot.binary_size is None:
            raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original has no exact sidecar binding")
        refusal = _refused(ledger, snapshot, config, usage=census())
        if refusal in media_allowance.ALLOWANCE_REFUSAL_CODES:
            _require_allowance(ledger, snapshot, refusal)
        if refusal is not None:
            return None
        if not snapshot.offloadable:
            return None
        if snapshot.reference is not None:
            head = _verified_head(backend, snapshot.reference)
            if head is not None and not head.condemned:
                with ExitStack() as stack:
                    source = (stack.enter_context(_open_local_original(vault, ledger._relative(path), snapshot))
                              if snapshot.state == "cached" else None)
                    if source is not None:
                        _bind_pin(ledger, token, snapshot, source, job=job)
                    if source is not None and _digest(source.stream) != ObjectHead(snapshot.binary_size, snapshot.binary_sha256):
                        raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original differs from its binding")
                    def confirm() -> None:
                        if source is not None:
                            source.require_current()
                        if original_state(vault, path) != snapshot:
                            raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the binding changed")
                        _publish_reference(vault, snapshot, snapshot.reference)
                        _finish_reference(ledger, original_state(vault, path), source)

                    _publishing(vault, "media_store_confirm_reference", confirm)
                return snapshot.reference
            # Only a definitive missing version permits removal, or a condemned one whose bytes are still here,
            # which then go up again under a new name. Unknown, denied or timed-out HEADs propagate and retain the
            # canonical reference, and so does a condemned version that is the only copy: it still hydrates.
            if head is not None and snapshot.state != "cached":
                raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the stored version is condemned and the bytes are absent")
            absent = snapshot

            def release() -> None:
                if original_state(vault, path) != absent:
                    raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the binding changed")
                assert absent.reference is not None
                _release_name(ledger, absent.reference.object)
                _publish_reference(vault, absent, None)

            _publishing(vault, "media_store_absent_version", release)
            snapshot = original_state(vault, path)
        if snapshot.state != "local":
            raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original bytes are absent")
        if _resident(vault, snapshot, config):
            # Refused before any temporary file exists: the rule needs only the sidecar's size.
            _refuse(ledger, snapshot, MediaStoreError(RESIDENT_CODE, "the original stays on the volume"),
                    media_capacity.age_ciphertext_size(snapshot.binary_size))
            return None
        decision, reuse = _offload_admission(vault, snapshot, usage=census())
        _require_allowance(ledger, snapshot, decision.code)
        key_version = str(config["key_version"])
        if reuse is not None and not _confirmed(backend, reuse.reference):
            reuse = None
        with _open_local_original(vault, path.relative_to(vault).as_posix(), snapshot) as source:
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
            if not _confirmed(backend, reference):
                # A condemned answer here means that a late attempt superseded this one; the retry uploads again.
                raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the uploaded version is not confirmed")
            def publish() -> None:
                source.require_current()
                if original_state(vault, path) != snapshot:
                    raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the binding changed")
                decision, _candidate = _offload_admission(
                    vault, snapshot, reference=reference, select_reuse=False, usage=census(),
                )
                _require_allowance(ledger, snapshot, decision.code)
                if batch is not None:
                    # Charged before the write: a failed write leaves the batch over-counting, never under.
                    batch.charge(ledger, snapshot, reference)
                _seal(ledger, snapshot, reference.object)
                _publish_reference(vault, snapshot, reference)
                _note_replica(vault, path, reference, None if reuse is not None else time.time())
                _finish_reference(ledger, original_state(vault, path), source)

            _publishing(vault, "media_store_publish", publish)
        return reference


# A replica copy that these codes refuse meets the same refusal when it is tried again: its bytes or its target decide.
# Any other failure is an outage or a change that a later attempt can pass. Closed codes that this module raises.
# nosemgrep: ep-word-set -- the closed set of refusal codes that replicate raises; they are not words a person typed.
REPLICA_REFUSAL_CODES = frozenset({
    "MEDIA_OBJECT_TOO_LARGE", "MEDIA_ORIGINAL_UNVERIFIED", "MEDIA_REPLICA_STORE_UNKNOWN", "MEDIA_REPLICA_STORE_REFUSED",
})


def require_replica_store(vault: Path, name: str) -> originals.StoreEntry:
    """The declared store `name`, when it can take a replica now; else `MediaStoreError` says why."""
    config = _settings(vault)
    if config.get("enabled") is not True:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the media store is disabled")
    entry = originals.store_entry(config, name)
    if entry is None:
        raise MediaStoreError("MEDIA_REPLICA_STORE_UNKNOWN", "no store of that name is declared")
    if entry.serves_eviction:
        raise MediaStoreError("MEDIA_REPLICA_STORE_REFUSED",
                              "offload fills the store that serves eviction; name another declared store")
    if not entry.available:
        fields = ", ".join((*entry.missing, *entry.invalid))
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", f"the store's descriptor is incomplete: {fields}")
    if (refusal := connection_refusal(entry)) is not None:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", refusal)
    return entry


def store_reachable(vault: Path, name: str) -> bool | None:
    """Whether the store answers as itself now; None when its adapter has no cheap check (a remote store is not probed)."""
    try:
        backend = store_for(vault, name)
    except (MediaStoreError, ValueError):
        return False
    if not isinstance(backend, FilesystemStore):
        return None
    try:
        backend.require_identity()
    except MediaStoreError:
        return False
    return True


def replica_unusable(vault: Path, replica: StoredObject) -> str | None:
    """Why automatic repair cannot read `replica` now, or None when it can.

    It needs a store that is available and `immediate`, and the decryption identity of the replica's key version.
    This install holds one identity, so a replica of another key version stays for the owner.
    """
    config = _settings(vault)
    entry = originals.store_entry(config, replica.store)
    if entry is None or not entry.available or connection_refusal(entry) is not None:
        return "store_unavailable"
    if entry.retrieval_class != originals.IMMEDIATE:
        return "owner_restores"
    identity = config.get("identity_path")
    if replica.key_version != str(config.get("key_version")) or not isinstance(identity, str) \
            or not os.access(identity, os.R_OK):
        return "identity_unreadable"
    return None


def _note_replica(vault: Path, path: Path, replica: StoredObject, verified_at: float | None) -> None:
    """Record a published replica in the derived table. A failure costs the row only: the next rebuild restores it."""
    from . import media_replicas

    try:
        media_replicas.record(vault, MediaJobStore(vault)._relative(path), replica, verified_at=verified_at)
    except (sqlite3.Error, OSError) as error:
        log.warning("a published replica is not yet in the replica table: %s", type(error).__name__)


def _copy_ciphertext(ledger: MediaJobStore, token: str, config: dict[str, object], vault: Path, backend: Store,
                     name: str, source: StoredObject) -> tuple[ObjectHead, str]:
    """Copy the eviction store's ciphertext of an evicted original into `backend` as `name`.

    The bytes are hashed on the way and must match the pointer's recorded size and digest, which its own upload
    confirmed. The key is not needed, and the plaintext never exists here.
    """
    with _ciphertext_reserved(ledger, token, config, source.ciphertext_size):
        with tempfile.TemporaryFile() as ciphertext:
            digest, size = hashlib.sha256(), 0
            with closing(_stored_chunks(store_for(vault), source)) as chunks:
                for chunk in chunks:
                    digest.update(chunk)
                    size += len(chunk)
                    ciphertext.write(chunk)
            head = ObjectHead(size, digest.hexdigest())
            if head != ObjectHead(source.ciphertext_size, source.ciphertext_sha256):
                raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the stored ciphertext differs from its pinned digest")
            ciphertext.seek(0)
            with _transport("upload"):
                return head, backend.upload(name, ciphertext, size=head.size, sha256=head.sha256)


def replicate(vault: Path, path: Path, store_name: str) -> StoredObject:
    """Make a verified replica of one bound original in the replica store `store_name`, and list it in its binding.

    The replica is ciphertext. An original with bytes on the volume is hashed against its binding, then encrypted to
    the vault's recipient. An evicted original is copied as the ciphertext that the eviction store holds, hashed on
    the way against its pointer. Either way the target store confirms the ciphertext's size and digest before the
    binding lists the replica. The sidecar write holds the vault boundary; hashing, encrypting and uploading do not.
    An existing replica in this store that still verifies is kept and nothing is written.
    """
    vault = Path(vault).resolve()
    config = _settings(vault)
    require_replica_store(vault, store_name)
    backend = store_for(vault, store_name)
    if isinstance(backend, FilesystemStore):
        # Before any read, hash, encryption or download: a store that does not answer as itself costs nothing.
        backend.require_identity()
    ledger = MediaJobStore(vault)
    with _pin(vault, path) as (_ledger, token), ExitStack() as held:
        snapshot = original_state(vault, path)
        if snapshot.binary_sha256 is None or snapshot.binary_size is None:
            raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original has no exact sidecar binding")
        listed = next((replica for replica in snapshot.replicas if replica.store == store_name), None)
        if listed is not None:
            head = _verified_head(backend, listed)
            if head is not None and not head.condemned:
                _note_replica(vault, path, listed, None)
                return listed
            # Only a definitive absence replaces the entry; an unknown answer raised above and keeps it.
        source = None
        if snapshot.state == "evicted":
            assert snapshot.reference is not None
        elif snapshot.state in ("local", "cached"):
            source = held.enter_context(_open_local_original(vault, path.relative_to(vault).as_posix(), snapshot))
            _bind_pin(ledger, token, snapshot, source)
            if _digest(source.stream) != ObjectHead(snapshot.binary_size, snapshot.binary_sha256):
                raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original differs from its binding")
            source.stream.seek(0)
        else:
            raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original bytes are absent")
        with _guard(vault, "media_store_replica_protect"):
            if source is not None:
                source.require_current()
            if original_state(vault, path) != snapshot:
                raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the binding changed")
            name = _upload_name(ledger, snapshot, store_name)
            _protect(ledger, snapshot, name, uploadable=True, replica_store=store_name)
        if source is not None:
            key_version = str(config["key_version"])
            head, version = _upload_encrypted(ledger, token, config, backend, name, source.stream, snapshot.binary_size)
        else:
            assert snapshot.reference is not None
            key_version = snapshot.reference.key_version
            head, version = _copy_ciphertext(ledger, token, config, vault, backend, name, snapshot.reference)
        replica = StoredObject(1, store_name, key_version, name, version, head.size, head.sha256)
        if not _confirmed(backend, replica):
            raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the uploaded version is not confirmed")

        def publish() -> None:
            if source is not None:
                source.require_current()
            if original_state(vault, path) != snapshot:
                raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the binding changed")
            _seal(ledger, snapshot, name)
            _commit_reference(vault, prepare_replica(vault, snapshot, replica))
            _note_replica(vault, path, replica, time.time())
            _durable(ledger, "DELETE FROM media_protections WHERE binary_rel = ? AND object = ?",
                     (ledger._relative(path), name))

        _publishing(vault, "media_store_replica_publish", publish)
    return replica


def _hydrate(vault: Path, snapshot: OriginalSnapshot, ledger: MediaJobStore, token: str) -> None:
    """Hydrate in one stream into one temporary file in the original's folder (D12, choice 3).

    The reservation names the temporary before it exists, so a stop at any point
    leaves a row that names its file, and nothing at the original's path.
    """
    from . import media_bindings, source_original_custody

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
        _hydrate_shared(vault, snapshot, ledger, token, key)
        return
    temporary = held_fs.publish_temporary_name()
    custody = None
    with ExitStack() as lifetime:
        try:
            custody = source_original_custody.prepare(vault, snapshot, ledger, token)
            _hydrate_reserved(vault, snapshot, ledger, token, temporary, lifetime=lifetime, custody=custody)
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
                    held_fs.Chunks(lambda: chunks), temporary_name=temporary, on_created=record_created) as prepared:
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
            plaintext = _decrypt_into(backend, snapshot.reference, str(config["identity_path"]), sink)
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


def _decrypted_chunks(backend: Store, reference: StoredObject, identity: str) -> Iterator[bytes]:
    """Stream plaintext; exhaustion verifies the ciphertext and age's authenticated exit."""
    process = subprocess.Popen(["age", "--decrypt", "--identity", identity],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    assert process.stdin is not None and process.stdout is not None
    ciphertext, fed, feed_errors = hashlib.sha256(), [0], []

    def feed() -> None:
        try:
            with closing(_stored_chunks(backend, reference)) as chunks:
                for chunk in chunks:
                    ciphertext.update(chunk)
                    fed[0] += len(chunk)
                    process.stdin.write(chunk)
        except BrokenPipeError:
            pass  # age stopped early; its exit status refuses the result.
        except BaseException as error:  # noqa: BLE001 - re-raised on the calling thread below.
            feed_errors.append(error)
        finally:
            with suppress(OSError):
                process.stdin.close()

    feeder = threading.Thread(target=feed, name="media-hydration-feed", daemon=True)
    feeder.start()
    try:
        # read1 returns what age has written so far, so the file grows as the download arrives.
        while chunk := process.stdout.read1(1024 * 1024):
            yield chunk
        returncode = process.wait()
    except BaseException:
        process.kill()
        process.wait()
        raise
    finally:
        feeder.join()
        process.stdout.close()
    if feed_errors:
        raise feed_errors[0]
    if returncode or ObjectHead(fed[0], ciphertext.hexdigest()) != ObjectHead(reference.ciphertext_size, reference.ciphertext_sha256):
        raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the ciphertext or its authenticated decryption failed")


def _decrypt_into(backend: Store, reference: StoredObject, identity: str, sink: BinaryIO) -> ObjectHead:
    """Download and decrypt in one pass; local sink failures remain local errors."""
    plaintext, size = hashlib.sha256(), 0
    with closing(_decrypted_chunks(backend, reference, identity)) as chunks:
        for chunk in chunks:
            plaintext.update(chunk)
            size += len(chunk)
            sink.write(chunk)
        sink.flush()
    return ObjectHead(size, plaintext.hexdigest())


def write_stored_original(vault: Path, reference: StoredObject, sink: BinaryIO, *, store: str | None = None) -> ObjectHead:
    """Decrypt one stored version into `sink` and return the plaintext's head; the export pack's read (D8).

    `store` names the declared store that holds the version, as a replica's pointer does; None reads the store that
    serves eviction.

    The pack reads a staging copy, not the live vault, so this takes no pin, reservation or ledger row. A
    transport failure raises `MEDIA_STORE_UNAVAILABLE`, after `sink` may have received a verified prefix.
    """
    return _decrypt_into(store_for(vault, store), reference, str(_settings(vault)["identity_path"]), sink)


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


def _stored_chunks(backend: Store, reference: StoredObject) -> Iterator[bytes]:
    # Only backend operations are transport failures; local staging writes keep their own errors.
    with _transport("read"), backend.open(reference.object, reference.version) as stream:
        remaining = reference.ciphertext_size
        while chunk := stream.read(min(1024 * 1024, remaining + 1)):
            if len(chunk) > remaining:
                raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the ciphertext exceeds its recorded size")
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

    assert snapshot.binary_sha256 is not None and snapshot.binary_size is not None
    bound = originals.BinaryBinding(relative, snapshot.binary_sha256, snapshot.binary_size)

    def require_binding() -> None:
        # The bound path, digest and size name the bytes; the companion's other fields, such as the
        # extraction text that a pinned job writes, may change while the bytes are held.
        current = originals.bound_companion(vault, relative)
        if (current is None or vault / current[0] != snapshot.sidecar_path
                or originals.binary_binding(current[1]) != bound):
            raise reserved_paths.ReservedPathLeafError("IDENTITY_CHANGED")

    try:
        require_binding()
        size = snapshot.binary_size if check_size else None
        with original_pool.open_object(vault, snapshot.binary_sha256, size) as source:
            def require_current() -> None:
                source.require_current()
                require_binding()

            yield reserved_paths.GenericBinarySource(source.stream, source.version, require_current, source.current_version)
            require_current()
    except held_fs.HeldFsError as error:
        raise reserved_paths.ReservedPathLeafError(error.code) from error


def pooled_head(vault: Path, relative: str, snapshot: OriginalSnapshot) -> ObjectHead:
    """The size and digest that a pooled filing's shared object holds now, read behind the filing's binding check."""
    with _open_local_original(vault, relative, snapshot) as source:
        head = _digest(source.stream)
        source.require_current()
    return head


def _read_state(vault: Path, target: Path) -> OriginalSnapshot:
    """The state that a read acts on; a retirement that its witness no longer confirms refuses as unverified."""
    try:
        return original_state(vault, target)
    except originals.RetirementUnconfirmed as error:
        raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", error.reason) from error


@contextmanager
def open_original(vault: Path, path: str | Path, *, physical: bool = False, read: bool = False,
                  job: MediaJob | None = None, deadline: float | None = None):
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
                snapshot = _read_state(vault, target)
                if snapshot.local_path is None:
                    raise error
            else:
                if job is not None:
                    source.require_current()
                    # The claimed job proves the ledger exists, so this store skips the schema setup.
                    if not MediaJobStore(vault, create=False).bind_original(job, relative):
                        raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the job no longer owns these original bytes")
                yield source
                return
    with _pin(vault, target, read=read) as (ledger, token):
        snapshot = _read_state(vault, target)
        if snapshot.state == "evicted":
            _hydrate(vault, snapshot, ledger, token)
            snapshot = _read_state(vault, target)
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
                  deadline: float | None = None):
    """Retain a logical original for checked whole-file and range delivery, including pooled bytes."""
    return reserved_paths._hold_binary_file(vault, path, open_original(vault, path, read=True, deadline=deadline),
                                           recorded_sha256=recorded_sha256, deadline=deadline)


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


def _evict_one(vault: Path, path: Path) -> str:
    config = _settings(vault)
    if not _may_evict(config):
        return KEPT
    ledger = MediaJobStore(vault)
    snapshot = _cached_original(vault, ledger, path)
    if isinstance(snapshot, str):
        return snapshot
    if snapshot.local_path is not None:
        return _evict_shared(vault, snapshot, ledger, config)
    bounds = media_capacity.bounds_for(config, vault)
    assert snapshot.binary_size is not None and snapshot.reference is not None
    if bounds is None or not bounds.hydration_fits(snapshot.binary_size):
        return KEPT  # An original that could not come back stays (D3).
    identity = _identity(vault, path)
    try:
        confirmed = _confirmed(store_for(vault), snapshot.reference)
    except MediaStoreError as error:
        # An unavailable or unverified version cannot authorize removal; an outage also ends the pass.
        return UNAVAILABLE if error.code == "MEDIA_STORE_UNAVAILABLE" else KEPT
    if not confirmed:
        # An absent or condemned version authorizes no removal: the bytes go up again under a new name.
        def release() -> None:
            if original_state(vault, path) == snapshot:
                assert snapshot.reference is not None
                _release_name(ledger, snapshot.reference.object)
                _publish_reference(vault, snapshot, None)
                enqueue_offload(vault, path)

        try:
            _publishing(vault, "media_store_absent_version", release)
        except MediaStoreError as error:
            if error.code != STORE_BUSY_CODE:
                raise
            return BUSY
        return KEPT
    with _guard(vault, "media_store_evict"):
        conn = ledger._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
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
                    or original_state(vault, path) != snapshot or _identity(vault, path) != identity):
                conn.rollback()
                return KEPT
            # Pin admission and enqueue use this same SQLite write exclusion.
            # Retained parent/file handles prevent an exchanged ancestor redirect.
            target = path.relative_to(vault)
            with held_fs.acquire(vault).require() as filesystem:
                with filesystem.parent(target.parent.as_posix(), access="mutate").require() as parent:
                    with filesystem.file(parent, target.name, access="mutate").require() as file:
                        if _identity(vault, path) != identity:
                            conn.rollback()
                            return KEPT
                        filesystem.unlink(file).require()
                        filesystem.flush_directory(parent).require()
            conn.execute("UPDATE media_cache SET evicted = 1 WHERE binary_rel = ?", (relative,))
            conn.commit()
            return EVICTED
        finally:
            conn.close()


def _evict_shared(vault: Path, snapshot: OriginalSnapshot, ledger: MediaJobStore, config: dict[str, object]) -> str:
    """Confirm distinct own recovery versions outside the guard; unlink once under physical pin exclusion."""
    from . import media_bindings, original_pool

    assert snapshot.local_path is not None and snapshot.binary_sha256 is not None and snapshot.binary_size is not None
    key = snapshot.local_path.relative_to(vault).as_posix()
    with closing(ledger._connect(readonly=True)) as conn:
        facts = media_bindings.object_facts(conn, key)
    bounds = media_capacity.bounds_for(config, vault)
    if (facts is None or facts.resident_only or not facts.recoverable or facts.pinned
            or bounds is None or not bounds.hydration_fits(facts.size)):
        return KEPT
    with original_pool.open_object(vault, facts.sha256, facts.size) as source:
        version = source.version
    try:
        backend = store_for(vault)
        if not all(_confirmed(backend, reference) for reference in facts.references):
            return KEPT
    except MediaStoreError as error:
        return UNAVAILABLE if error.code == "MEDIA_STORE_UNAVAILABLE" else KEPT
    with _guard(vault, "media_store_evict_shared"), closing(ledger._connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _clear_dead_access(conn, ledger, conn.execute(
            "SELECT * FROM media_access WHERE object_key = ?", (key,)).fetchall())
        latest = media_bindings.object_facts(conn, key)
        if (latest is None or latest.checkpoint != facts.checkpoint or not media_bindings.current(vault, conn)
                or latest.sha256 != facts.sha256 or latest.size != facts.size or latest.references != facts.references
                or latest.resident_only or not latest.recoverable or latest.pinned
                or latest.owner_read is not None and time.time() - latest.owner_read < float(config["minimum_idle_seconds"])):
            conn.rollback()
            return KEPT
        original_pool.evict_object(vault, facts.sha256, expected_version=version)
        conn.execute("UPDATE media_cache SET evicted = 1 WHERE binary_rel = ?", (key,))
        conn.commit()
        return EVICTED


# A demand lapses 15 minutes after its series last renewed it (D3).
DEMAND_LEASE_SECONDS = 15 * 60
# Each read takes at most this many candidates, so a pass never loads the whole cache at once.
_EVICTION_BATCH = 64
# Unread originals first, those with no pending job before those with one, each largest first;
# then owner-read originals, largest first and longest idle on ties (D3, M4). The path makes the
# order total, so a pass reads past the candidates it keeps by their count.
def _cache_selection(vault: Path, conn, parameters: dict[str, object]) -> tuple[str, str, dict[str, object]]:
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
                    "EXISTS (SELECT 1 FROM media_bindings b WHERE b.object_key = media_cache.binary_rel) AND "
                    "NOT EXISTS (SELECT 1 FROM media_bindings b WHERE b.object_key = media_cache.binary_rel "
                    "AND (b.resident_only = 1 OR b.reference IS NULL OR b.sha256 != media_cache.sha256 "
                    "OR b.size != media_cache.size))))")
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


def evict_to_target(vault: Path) -> int:
    """Evict above the everyday target, or while a live demand can be met (D3, M3)."""
    vault = Path(vault).resolve()
    config = originals.configuration(vault)
    if config is None or not _may_evict(config):
        return 0
    if config["enabled"] is False:
        # A retained store drains once its evicted originals fit, so a copy that fits stays (D12). Eviction stops
        # without the margin, which leaves the drain's band. An unknown figure evicts as a served cell does:
        # `evict` re-confirms the stored version first.
        try:
            if _retained_gap(vault, config, margin=False) == 0:
                return 0
        except MediaStoreError:
            pass
    bounds = media_capacity.bounds_for(config, vault)
    if bounds is None:
        return 0
    target = float(config["eviction_target"])
    margin = float(config["eviction_margin"])
    if not 0 < margin < target < 1:
        raise ValueError("media eviction fractions require 0 < margin < target < 1")
    usage = shutil.disk_usage(vault)
    everyday = usage.total * (target - margin) if usage.used > usage.total * target else None
    ledger = MediaJobStore(vault)
    demand = _live_demand(ledger)

    def needed(used: int) -> tuple[bool, bool]:
        return (everyday is not None and used > everyday,
                demand is not None and demand[1] - used < demand[0])

    # `size <= :hydration` is the SQL form of `Bounds.hydration_fits`, which `evict` checks again.
    parameters = {"hydration": bounds.hydration_bytes,
                  "idle_before": time.time() - float(config["minimum_idle_seconds"])}
    _refresh_candidates(ledger)
    used = shutil.disk_usage(vault).used
    above_target, short = needed(used)
    if not above_target and short:
        assert demand is not None
        if not _room_covers(vault, ledger, parameters, demand[0] - (demand[1] - used)):
            return 0  # A demand that the evictable originals cannot cover yet evicts nothing.
    count = kept = 0
    while any(needed(shutil.disk_usage(vault).used)) and (page := _candidates(ledger, parameters, kept)):
        for relative, _size in page:
            if not any(needed(shutil.disk_usage(vault).used)):
                break
            outcome = _evict_one(vault, vault / relative)
            if outcome in (UNAVAILABLE, BUSY):
                # Each later candidate would wait for the same store or boundary: the next pass
                # asks again.
                return count
            # An evicted or pruned row leaves the candidates; a kept one stays, so the next page starts after it.
            count += outcome == EVICTED
            kept += outcome == KEPT
    return count


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






def drain_census(vault: Path) -> tuple[int, int]:
    """Count physical objects off-volume; publish only an unchanged complete binding frontier."""
    from . import media_bindings

    vault = Path(vault).resolve()
    config, binding = originals.configuration(vault), originals.binding_for(vault)
    if config is None or binding is None:
        raise MediaStoreError("MEDIA_CENSUS_UNKNOWN", "the vault has no media store")
    ledger = MediaJobStore(vault)
    now = datetime.now(UTC)
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
            _physical_drain_census(vault, conn)
            count, size = conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) "
                                       "FROM drain_census WHERE present = 0").fetchone()
            with _guard(vault, "media_drain_census_publish"):
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    if (media_bindings.checkpoint(conn) != before or not media_bindings.current(vault, conn)
                            or media_bindings.policy_key(vault) != policy):
                        raise ValueError("the canonical bindings changed during the census")
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
        with suppress(*_LEDGER_UNREADABLE), closing(ledger._connect()) as conn, conn:
            conn.execute("INSERT INTO media_drain(singleton, attempted_at, attempt_failed, blocked_by) VALUES (1, ?, 1, ?) "
                         "ON CONFLICT(singleton) DO UPDATE SET attempted_at = excluded.attempted_at, attempt_failed = 1, "
                         "blocked_by = excluded.blocked_by",
                         (now.isoformat(timespec="seconds"),
                          json.dumps(error.blocked) if isinstance(error, _CompanionBlocks) else None))
        raise MediaStoreError("MEDIA_CENSUS_UNKNOWN", f"the companions or the ledger cannot be read ({type(error).__name__})") from error
    # A committed latch precedes its externally consumed three-token record.
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
    if config is None or config.get("enabled") is not False or config.get("eviction_enabled") is not True:
        return 0, None
    threshold, bounds = media_capacity.growth_threshold(config), media_capacity.bounds_for(config, vault)
    try:
        margin, idle = float(config["eviction_margin"]), float(config["minimum_idle_seconds"])
    except (KeyError, TypeError, ValueError):
        return 0, None
    record = drain_record(vault)
    if threshold is None or bounds is None or (record is not None and record.latched):
        return 0, None
    if retained_shortfall(vault, config):
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
    if config is None or config.get("enabled") is not False:
        return
    try:
        drain_step(vault, claimable)
    except Exception:  # noqa: BLE001 - a failed step hydrates nothing; the census below is what repairs a missing size
        log.warning("media drain: hydration step skipped", exc_info=True)
    if _census_due(vault):
        drain_census(vault)
