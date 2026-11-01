"""Encrypted, version-pinned originals; the volume is a verified local cache.

A store is selected only by explicit configuration: the media edge for Cloud cells,
and a filesystem adapter for tests. No store is selected by default. Jobs share the
media ledger; protection records outlive those jobs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, closing, contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol

from . import (
    held_fs,
    media_allowance,
    media_capacity,
    media_drain_record,
    originals,
    reserved_paths,
    trash_metadata,
)
from .media_jobs import (
    OFFLOAD_MEDIA_TYPE,
    STATUS_JOB_LIMIT,
    MediaJob,
    MediaJobStore,
    holder_alive,
    process_start,
)
from .originals import OriginalSnapshot, StoredObject, original_state

log = logging.getLogger(__name__)


class MediaStoreError(RuntimeError):
    def __init__(self, code: str, reason: str):
        self.code = code
        super().__init__(f"{code}: {reason}")


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


@dataclass(frozen=True, slots=True)
class ObjectHead:
    size: int
    sha256: str


class Store(Protocol):
    # The declared size and digest let the store refuse before or instead of publishing (D6).
    def upload(self, name: str, stream: BinaryIO, *, size: int, sha256: str) -> str: ...
    def head(self, name: str, version: str) -> ObjectHead | None: ...
    def open(self, name: str, version: str): ...


def _digest(stream: BinaryIO) -> ObjectHead:
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    return ObjectHead(size, digest.hexdigest())


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


class FilesystemStore:
    """Versioned ciphertext store for isolated tests, with no deletion API."""

    def __init__(self, root: Path, *, max_object_bytes: int | None = None):
        self.root = root
        self.max_object_bytes = max_object_bytes

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
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with held_fs.acquire(self.root).require() as filesystem:
            with filesystem.parent(name, create=True, access="mutate").require() as parent:
                held_fs.publish_bytes(filesystem, parent, version,
                                      held_fs.Chunks(lambda: _declared_chunks(stream, size, sha256))).require()
                filesystem.flush_directory(parent).require()
        return version

    @contextmanager
    def open(self, name: str, version: str):
        relative = self._path(name, version)
        with held_fs.acquire(self.root).require() as filesystem:
            with filesystem.parent(name).require() as parent:
                with filesystem.file(parent, Path(relative).name).require() as file:
                    with os.fdopen(os.dup(file.descriptor), "rb") as stream:
                        yield stream

    def head(self, name: str, version: str) -> ObjectHead | None:
        self._path(name, version)
        # A missing store root is unavailable, not proof that a version is gone.
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


def store_for(vault: Path) -> Store:
    config = _settings(vault)
    if config.get("adapter") == "media-edge":
        from .media_edge_store import EdgeStore

        return EdgeStore.from_configuration(config)
    if config.get("adapter") != "filesystem-test":
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the media store adapter is unavailable")
    bound = config.get("max_object_bytes")
    return FilesystemStore(Path(str(config["root"])), max_object_bytes=None if bound is None else int(bound))


@contextmanager
def _transport(operation: str):
    """A store transport fault is an outage, which leaves offload demand retryable."""
    try:
        yield
    except (held_fs.HeldFsError, OSError) as error:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", f"the media store {operation} failed: {error}") from error


def _reference_key(reference: StoredObject) -> str:
    return json.dumps(reference.fields(), sort_keys=True, separators=(",", ":"))


def _source_identity(source: reserved_paths.GenericBinarySource) -> str:
    version = source.version
    return json.dumps((version.identity.device, version.identity.inode, version.change_time_ns,
                       version.mtime_ns, version.size), separators=(",", ":"))


def _identity(vault: Path, path: Path) -> str:
    with reserved_paths.open_generic_binary(vault, path.relative_to(vault).as_posix()) as source:
        return _source_identity(source)


def cache_matches(vault: Path, path: Path, *, snapshot: OriginalSnapshot | None = None) -> bool:
    """Read a proven cache identity without creating state or a store client."""
    selected = snapshot if snapshot is not None else original_state(vault, path)
    if selected.state != "cached" or selected.reference is None:
        return False
    store = MediaJobStore(vault, create=False)
    if not store.path.exists():
        return False
    conn = store._connect(readonly=True)
    try:
        if conn.execute("SELECT 1 FROM sqlite_schema WHERE name = 'media_cache' AND type = 'table'").fetchone() is None:
            return False
        row = conn.execute("SELECT * FROM media_cache WHERE binary_rel = ?", (store._relative(path),)).fetchone()
        return row is not None and row["reference"] == _reference_key(selected.reference) and row["sha256"] == selected.binary_sha256 and row["identity"] == _identity(vault, path)
    finally:
        conn.close()


def recover_cache(vault: Path, path: Path, snapshot: OriginalSnapshot) -> bool:
    if snapshot.state != "cached" or snapshot.reference is None:
        return False
    with _pin(vault, path) as (ledger, _token):
        with reserved_paths.open_generic_binary(vault, ledger._relative(path)) as source:
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
    # `last_read` is the legacy record time; only `owner_read` decides idleness (D3, M4).
    conn.execute(
        "INSERT INTO media_cache(binary_rel, reference, sha256, identity, last_read, size, evicted) "
        "VALUES (?, ?, ?, ?, ?, ?, 0) "
        "ON CONFLICT(binary_rel) DO UPDATE SET reference=excluded.reference, sha256=excluded.sha256, "
        "identity=excluded.identity, last_read=excluded.last_read, size=excluded.size, evicted=0",
        (store._relative(snapshot.path), _reference_key(snapshot.reference), snapshot.binary_sha256,
         _source_identity(source), time.time(), snapshot.binary_size),
    )


def _clear_dead_access(conn, store: MediaJobStore, rows) -> int:
    """Delete the rows of dead processes, and remove the temporary file that each one names."""
    cleared = 0
    for row in rows:
        if holder_alive(row["pid"], row["pid_start"]):
            continue
        # Only a reserved publish name is removed, so a damaged row cannot name an original.
        temporary = row["temporary_name"]
        if temporary is not None and held_fs.is_publish_temporary(temporary) and not _remove_temporary(
                store.vault_root, row["binary_rel"], temporary):
            continue  # The row keeps its bytes reserved until a later clear removes the file.
        conn.execute("DELETE FROM media_access WHERE token = ?", (row["token"],))
        cleared += 1
    return cleared


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
def _pin(vault: Path, path: Path, *, read: bool = False):
    """Keep an original on the volume; `read` marks an owner's playback, download or viewing (D3, M4)."""
    store = MediaJobStore(vault)
    token = uuid.uuid4().hex
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO media_access(token, binary_rel, pid, pid_start) VALUES (?, ?, ?, ?)",
                     (token, store._relative(path), os.getpid(), process_start(os.getpid())))
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
                    conn.execute("UPDATE media_cache SET owner_read = ? WHERE binary_rel = ?",
                                 (time.time(), store._relative(path)))
                # A row that still names a temporary keeps it reserved until a clear removes the file.
                conn.execute("DELETE FROM media_access WHERE token = ? AND temporary_name IS NULL", (token,))
        finally:
            conn.close()


def _reserve(store: MediaJobStore, token: str, needed: int, limits: media_capacity.HydrationLimits, *,
             hydration: bool, temporary: str | None = None) -> None:
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
        conn.execute("BEGIN IMMEDIATE")
        _clear_dead_access(conn, store, conn.execute(
            "SELECT * FROM media_access WHERE temporary_bytes > 0 OR hydrating = 1").fetchall())
        row = conn.execute("SELECT COALESCE(SUM(temporary_bytes), 0), COALESCE(SUM(hydrating), 0) "
                           "FROM media_access WHERE temporary_bytes > 0 OR hydrating = 1").fetchone()
        busy = needed + row[0] > maximum or hydration and row[1] >= concurrent
        if not busy:
            conn.execute("UPDATE media_access SET temporary_bytes = ?, hydrating = ?, temporary_name = ? "
                         "WHERE token = ?", (needed, int(hydration), temporary, token))
        conn.commit()  # The cleared rows stay cleared when the capacity is busy.
    finally:
        conn.close()
    if busy:
        raise MediaStoreError(STORE_BUSY_CODE, "hydration capacity is in use")


def managed_event(vault: Path, path: Path) -> str | None:
    """Tell the watcher to defer an in-flight event or suppress proven cache bytes."""
    if originals.binding_for(vault) is None:
        return None
    relative = Path(path).relative_to(vault).as_posix()
    if reserved_paths.classify_logical(relative).blocked:
        return None
    store = MediaJobStore(vault, create=False)
    if store.path.exists():
        conn = store._connect(readonly=True)
        try:
            if conn.execute("SELECT 1 FROM sqlite_schema WHERE name = 'media_access' AND type = 'table'").fetchone() is not None:
                rows = conn.execute("SELECT * FROM media_access WHERE binary_rel = ?", (store._relative(path),)).fetchall()
                # A read-only reader can see a ledger from before the start column, which the next write adds.
                if any(holder_alive(row["pid"], row["pid_start"] if "pid_start" in row.keys() else None) for row in rows):
                    return "defer"
        finally:
            conn.close()
    snapshot = original_state(vault, path)
    if snapshot.state == "evicted" or cache_matches(vault, path, snapshot=snapshot):
        return "suppress"
    return None


def bound_originals(vault: Path, *, root: Path | None = None) -> Iterator[OriginalSnapshot]:
    """Discover sidecar bindings, including unsupported or physically absent originals."""
    from .backfill import iter_kb_files
    from .kbdir import kb_dirname
    from .vault import parse_frontmatter

    root = root if root is not None else vault / kb_dirname()
    if not root.is_dir():
        return
    for sidecar in iter_kb_files(root):
        if sidecar.suffix.lower() != ".md":
            continue
        try:
            raw = reserved_paths.read_generic_bytes(vault, sidecar.relative_to(vault).as_posix())
            fields, _body, _marker = parse_frontmatter(raw.data.decode("utf-8"), custody=True)
            relative = originals.bound_path(fields)
            if not isinstance(relative, str):
                continue
            snapshot = original_state(vault, relative)
            if snapshot.sidecar_path == sidecar and snapshot.binary_sha256 is not None:
                yield snapshot
        except (UnicodeError, ValueError):
            continue


def _status_candidates(vault: Path, table: str, query: str, parameters: tuple[object, ...] = ()) -> tuple[list[sqlite3.Row], dict[str, object]]:
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


def processing_status(vault: Path, *, path: str | None = None) -> tuple[list[dict[str, object]], dict[str, object]]:
    if path is not None:
        return [_processing_status_row(vault, path)], {"source": "sidecar", "coverage": "exact-path"}
    candidates, coverage = _status_candidates(vault, "media_cache",
        "SELECT binary_rel FROM media_cache ORDER BY binary_rel LIMIT ?")
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
    from .writer_lease import get_manager

    if originals.binding_for(vault) is None:
        return 0
    with get_manager().consistency_guard(vault, operation="media_offload_scan", holder_kind="background"):
        placeholders = ",".join("?" for _code in media_allowance.ALLOWANCE_REFUSAL_CODES)
        rows, _coverage = _status_candidates(vault, "media_offload_refusals",
            f"SELECT binary_rel FROM media_offload_refusals WHERE code IN ({placeholders}) LIMIT ?",
            media_allowance.ALLOWANCE_REFUSAL_CODES)
        usage = media_allowance.reference_usage(vault) if rows else None
        return sum(_enqueue_offload(vault, item.path, usage=usage) for item in bound_originals(vault))


def _guard(vault: Path, operation: str):
    from .writer_lease import get_manager

    return get_manager().mutation_guard(vault, operation=operation, holder_kind="background")


def _binding_of(vault: Path, snapshot: OriginalSnapshot) -> originals.BinaryBinding:
    assert snapshot.binary_sha256 is not None and snapshot.binary_size is not None
    return originals.BinaryBinding(snapshot.path.relative_to(vault).as_posix(),
                                   snapshot.binary_sha256, snapshot.binary_size)


class OffloadBatch:
    """One owner census for a run of offloads, kept across guard releases (D10).

    The batch never holds the mutation boundary, so other writes run between two members. It rests
    on offload being the only writer that binds a version the census has not seen while the runtime
    serves (task 5.4.9 lists the others). `charge` adds each version that a member publishes. `end`
    drops the census so that the next member reads it again, and `failed` does so only for a member
    that reached `charge`: a refused member bound nothing, so the next one reuses the census. A stale
    census can wrongly refuse a member, which the next scan releases, because refusals are read again
    from a fresh census.
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
            "INSERT INTO media_owner_census(singleton, used_bytes, observed_at) VALUES (1, ?, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET used_bytes=excluded.used_bytes, observed_at=excluded.observed_at",
            (usage.used_bytes, usage.observed_at),
        )


@dataclass(frozen=True, slots=True)
class RecordedCensus:
    """An owner census as recorded: the bytes in use, or None when it could not count, and when it was read."""

    used_bytes: int | None
    observed_at: str

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
    return RecordedCensus(usage.used_bytes, usage.observed_at)


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
            row = conn.execute("SELECT used_bytes, observed_at FROM media_owner_census").fetchone()
    except _LEDGER_UNREADABLE as error:
        raise MediaStoreError("MEDIA_LEDGER_UNREADABLE", f"the media ledger cannot be read: {error}") from error
    return None if row is None else RecordedCensus(None if row["used_bytes"] is None else int(row["used_bytes"]), str(row["observed_at"]))


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


def _publish_reference(vault: Path, snapshot: OriginalSnapshot, reference: StoredObject | None) -> None:
    from . import preserve
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
    if reference is None:
        fields.pop("stored_object", None)
    else:
        fields["stored_object"] = reference.fields()
    rendered = render_frontmatter_document(serialize_frontmatter(fields), body)
    preserve.commit_media_sidecar_writes(vault, (PlannedWrite(
        snapshot.sidecar_path, rendered, guard=guard, expected_hash=content_hash(text),
    ),))
    # Keep protection through an uncertain writer or catalog result. Explicitly
    # confirm durable bytes even when a prior attempt already wrote this value.
    with reserved_paths.open_generic_binary(vault, snapshot.sidecar_path.relative_to(vault).as_posix()) as source:
        if source.stream.read() != rendered.encode("utf-8"):
            raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "reference publication is uncertain")
        os.fsync(source.stream.fileno())
    held_fs.flush_directory_path(snapshot.sidecar_path.parent).require()


def _confirmed(store: Store, reference: StoredObject) -> bool:
    with _transport("head"):
        head = store.head(reference.object, reference.version)
    if head is None:
        return False
    if head != ObjectHead(reference.ciphertext_size, reference.ciphertext_sha256):
        raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the pinned ciphertext differs")
    return True


def forget_original(vault: Path, path: str | Path) -> None:
    """Remove a deleted original's binding before its explicit deletion fan-out."""
    if originals.binding_for(vault) is None:
        return
    snapshot = original_state(vault, path)
    if snapshot.reference is None:
        return
    with _guard(vault, "media_store_forget_original"):
        if original_state(vault, path) != snapshot:
            raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the deletion binding changed")
        _publish_reference(vault, snapshot, None)


def _protect(store: MediaJobStore, snapshot: OriginalSnapshot, name: str) -> None:
    conn = store._connect()
    try:
        # D9 requires FULL before BEGIN: NORMAL can omit this record from a clone.
        conn.execute("PRAGMA synchronous=FULL")
        with conn:
            # One record per object covers every retried attempt for this original.
            conn.execute("INSERT INTO media_protections(token, binary_rel, object, created_at) "
                         "SELECT ?, ?, ?, ? WHERE NOT EXISTS (SELECT 1 FROM media_protections "
                         "WHERE binary_rel = ? AND object = ?)",
                         (uuid.uuid4().hex, store._relative(snapshot.path), name, time.time(),
                          store._relative(snapshot.path), name))
    finally:
        conn.close()


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
            result.append({"path": row["binary_rel"], "original_state": snapshot.state,
                           "code": code, "ciphertext_size": row["ciphertext_size"]})
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


def offload(vault: Path, path: Path, *, batch: OffloadBatch | None = None) -> StoredObject | None:
    """Protect, encrypt, confirm and durably publish a bound original.

    None means that the original stays local: notes and datasets are never
    offloaded. Quota refusals raise so that the worker retains retryable demand (D10).
    A `batch` shares its census with this member's checks; without one, each check reads its own.
    """
    if batch is not None:
        batch.begin()
    try:
        return _offload_member(Path(vault).resolve(), path, batch)
    except BaseException:
        if batch is not None:
            batch.failed()
        raise


def _offload_member(vault: Path, path: Path, batch: OffloadBatch | None) -> StoredObject | None:
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
            if _confirmed(backend, snapshot.reference):
                with ExitStack() as stack:
                    source = (stack.enter_context(reserved_paths.open_generic_binary(vault, ledger._relative(path)))
                              if snapshot.state == "cached" else None)
                    if source is not None and _digest(source.stream) != ObjectHead(snapshot.binary_size, snapshot.binary_sha256):
                        raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original differs from its binding")
                    with _guard(vault, "media_store_confirm_reference"):
                        if source is not None:
                            source.require_current()
                        if original_state(vault, path) != snapshot:
                            raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the binding changed")
                        _publish_reference(vault, snapshot, snapshot.reference)
                        _finish_reference(ledger, original_state(vault, path), source)
                return snapshot.reference
            # Only a definitive missing version permits removal. Unknown, denied
            # or timed-out HEADs propagate and retain the canonical reference.
            with _guard(vault, "media_store_absent_version"):
                if original_state(vault, path) != snapshot:
                    raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the binding changed")
                _publish_reference(vault, snapshot, None)
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
        name_key = Path(str(config["name_key_path"])).read_bytes()
        if len(name_key) < 32:
            raise ValueError("the media name key requires at least 32 bytes")
        name = hmac.new(name_key, bytes.fromhex(snapshot.binary_sha256), hashlib.sha256).hexdigest()
        key_version = str(config["key_version"])
        if reuse is not None and not _confirmed(backend, reuse.reference):
            reuse = None
        with reserved_paths.open_generic_binary(vault, path.relative_to(vault).as_posix()) as source:
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
                _protect(ledger, snapshot, reuse.reference.object if reuse is not None else name)
            if reuse is not None:
                reference = reuse.reference
                assert reference is not None
            else:
                # The ciphertext temporary file shares the hydration capacity and takes no slot (D12, choice 3).
                # A policy with no capacity reserves nothing, as before the capacity existed.
                limits = media_capacity.hydration_limits(config)
                if limits is not None:
                    _reserve(ledger, token, media_capacity.age_ciphertext_size(snapshot.binary_size), limits,
                             hydration=False)
                try:
                    with tempfile.TemporaryFile() as ciphertext:
                        result = subprocess.run(["age", "--encrypt", "--recipient", str(config["recipient"])],
                                                stdin=source.stream, stdout=ciphertext, stderr=subprocess.PIPE,
                                                check=False)
                        if result.returncode:
                            raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "age encryption failed")
                        ciphertext.seek(0)
                        head = _digest(ciphertext)
                        ciphertext.seek(0)
                        try:
                            with _transport("upload"):
                                version = backend.upload(name, ciphertext, size=head.size, sha256=head.sha256)
                        except ObjectTooLarge as refusal:
                            _refuse(ledger, snapshot, refusal, head.size)
                            return None
                        reference = StoredObject(1, str(config["store"]), key_version, name, version, head.size,
                                                 head.sha256)
                finally:
                    _release(ledger, token)
            if not _confirmed(backend, reference):
                raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the uploaded version is not confirmed")
            with _guard(vault, "media_store_publish"):
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
                _publish_reference(vault, snapshot, reference)
                _finish_reference(ledger, original_state(vault, path), source)
        return reference


def _hydrate(vault: Path, snapshot: OriginalSnapshot, ledger: MediaJobStore, token: str) -> None:
    """Hydrate in one stream into one temporary file in the original's folder (D12, choice 3).

    The reservation names the temporary before it exists, so a stop at any point
    leaves a row that names its file, and nothing at the original's path.
    """
    temporary = held_fs.publish_temporary_name()
    try:
        _hydrate_reserved(vault, snapshot, ledger, token, temporary)
    finally:
        _release(ledger, token, temporary_gone=_remove_temporary(vault, ledger._relative(snapshot.path), temporary))


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
                         "CASE WHEN :gone THEN NULL ELSE temporary_name END WHERE token = :token",
                         {"gone": temporary_gone, "token": token})
    finally:
        conn.close()


def _hydrate_reserved(vault: Path, snapshot: OriginalSnapshot, ledger: MediaJobStore, token: str,
                      temporary: str) -> None:
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
    with held_fs.acquire(vault).require() as filesystem:
        with filesystem.parent(relative.parent.as_posix(), access="mutate").require() as parent:
            try:
                with filesystem.file(parent, temporary, access="write", create=True, exclusive=True).require() as file:
                    written = file.identity
                    with os.fdopen(os.dup(file.descriptor), "wb") as sink:
                        plaintext = _decrypt_into(backend, snapshot.reference, str(config["identity_path"]), sink)
                    os.fsync(file.descriptor)
                if plaintext != ObjectHead(snapshot.binary_size, snapshot.binary_sha256):
                    raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original failed authenticated verification")
                # Only the link runs under the vault guard; the download and the decryption ran outside it.
                with _guard(vault, "media_store_hydrate"):
                    if original_state(vault, snapshot.path) != snapshot:
                        raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the original changed during hydration")
                    recheck_path_guards(vault, (guard,))
                    held_fs.install_temporary(filesystem, parent, temporary, relative.name,
                                              expected_identity=written).require()
                    filesystem.flush_directory(parent).require()
                    with reserved_paths.open_generic_binary(vault, relative.as_posix()) as placed:
                        # The verified temporary is now the original; its inode proves it without a second hash.
                        if (placed.version.identity.device, placed.version.identity.inode) != (written.device, written.inode):
                            raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the placed original changed")
                        conn = ledger._connect()
                        try:
                            with conn:
                                _record_cache(conn, ledger, original_state(vault, snapshot.path), placed)
                        finally:
                            conn.close()
            except BaseException:
                # The held folder reaches the temporary even after a move renamed its path.
                with suppress(held_fs.HeldFsError, OSError):
                    with filesystem.file(parent, temporary, access="mutate").require() as file:
                        filesystem.unlink(file).require()
                raise


def _decrypt_into(backend: Store, reference: StoredObject, identity: str, sink: BinaryIO) -> ObjectHead:
    """Download, decrypt and write in one pass; return the plaintext's head once both checks pass.

    A thread feeds the download to `age` while this thread writes its output, so a
    full local disk raises here as a local error, never as a store or verification failure.
    """
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
    plaintext, size = hashlib.sha256(), 0
    try:
        # read1 returns what age has written so far, so the file grows as the download arrives.
        while chunk := process.stdout.read1(1024 * 1024):
            plaintext.update(chunk)
            size += len(chunk)
            sink.write(chunk)
        sink.flush()
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
    return ObjectHead(size, plaintext.hexdigest())


def _stored_chunks(backend: Store, reference: StoredObject) -> Iterator[bytes]:
    # Only backend operations are transport failures; local staging writes keep their own errors.
    with _transport("read"), backend.open(reference.object, reference.version) as stream:
        remaining = reference.ciphertext_size
        while chunk := stream.read(min(1024 * 1024, remaining + 1)):
            if len(chunk) > remaining:
                raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the ciphertext exceeds its recorded size")
            remaining -= len(chunk)
            yield chunk


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
    scan_roots = {target if target.is_dir() else target.parent
                  for prefix in prefixes for target in (root / prefix,)}
    seen: set[Path] = set()
    with ExitStack() as stack:
        for snapshot in (item for scan_root in scan_roots for item in bound_originals(root, root=scan_root)):
            if snapshot.path in seen:
                continue
            seen.add(snapshot.path)
            relative = snapshot.path.relative_to(root).as_posix()
            if not any(relative == prefix or relative.startswith(prefix + "/") for prefix in prefixes):
                continue
            ledger, token = stack.enter_context(_pin(root, snapshot.path, read=read))
            current = original_state(root, snapshot.path)
            if current.state == "evicted":
                _hydrate(root, current, ledger, token)
        yield


@contextmanager
def open_original(vault: Path, path: str | Path, *, physical: bool = False, read: bool = False):
    """Pin before resolution; retain verified bytes through the caller's read.

    `read` marks an owner's playback or viewing; processing and imports record no read.
    """
    vault = Path(vault).resolve()
    target = Path(path)
    if not target.is_absolute():
        target = vault / target
    relative = target.relative_to(vault).as_posix()
    if originals.binding_for(vault) is None:
        with reserved_paths.open_generic_binary(vault, relative, physical=physical) as source:
            yield source
        return
    with _pin(vault, target, read=read) as (ledger, token):
        snapshot = original_state(vault, target)
        if snapshot.state == "evicted":
            _hydrate(vault, snapshot, ledger, token)
            snapshot = original_state(vault, target)
        with reserved_paths.open_generic_binary(vault, relative, physical=physical) as source:
            if snapshot.reference is not None and not cache_matches(vault, target, snapshot=snapshot):
                if _digest(source.stream) != ObjectHead(snapshot.binary_size, snapshot.binary_sha256):
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


# The outcomes of one eviction attempt, a closed set that `evict_to_target` branches on (D3, M3).
EVICTED, KEPT, PRUNED, UNAVAILABLE = "evicted", "kept", "pruned", "unavailable"
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
    the write exclusion keep them all out. An evicted path keeps its row, marked.
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
            if snapshot.state == "evicted":
                # The flag orders the claims of the original's jobs (D3, M4).
                conn.execute("UPDATE media_cache SET evicted = 1 WHERE binary_rel = ?", (relative,))
            else:
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
        with _guard(vault, "media_store_absent_version"):
            if original_state(vault, path) == snapshot:
                _publish_reference(vault, snapshot, None)
                enqueue_offload(vault, path)
        return KEPT
    with _guard(vault, "media_store_evict"):
        conn = ledger._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            relative = ledger._relative(path)
            _clear_dead_access(conn, ledger, conn.execute(
                "SELECT * FROM media_access WHERE binary_rel = ?", (relative,)).fetchall())
            if conn.execute("SELECT 1 FROM media_drain WHERE latched = 1").fetchone():
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


# A demand lapses 15 minutes after its series last renewed it (D3).
DEMAND_LEASE_SECONDS = 15 * 60
# Each read takes at most this many candidates, so a pass never loads the whole cache at once.
_EVICTION_BATCH = 64
_ELIGIBLE = (
    "FROM media_cache WHERE evicted = 0 AND size IS NOT NULL AND size <= :hydration "
    "AND (owner_read IS NULL OR owner_read <= :idle_before) "
    "AND NOT EXISTS (SELECT 1 FROM media_access WHERE media_access.binary_rel = media_cache.binary_rel) "
    f"AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.binary_rel = media_cache.binary_rel AND {_JOB_PINS})"
)
# Unread originals first, those with no pending job before those with one, each largest first;
# then owner-read originals, largest first and longest idle on ties (D3, M4). The path makes the
# order total, so a pass reads past the candidates it keeps by their count.
_EVICTION_ORDER = (
    "ORDER BY owner_read IS NOT NULL, "
    "CASE WHEN owner_read IS NULL THEN EXISTS (SELECT 1 FROM jobs WHERE jobs.binary_rel = media_cache.binary_rel "
    "AND jobs.state = 'pending') ELSE 0 END, size DESC, owner_read, binary_rel"
)


# An offload job that no claimed, unfinished job pins; a pinned original is counted as job-pinned instead.
_UNPINNED_OFFLOAD = (
    "SELECT DISTINCT offload.binary_rel FROM jobs AS offload WHERE offload.media_type = :offload "
    "AND offload.do_offload = 1 AND offload.state IN ('pending', 'running') "
    f"AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.binary_rel = offload.binary_rel AND {_JOB_PINS})"
)
_PINNED_ORIGINALS = f"SELECT DISTINCT jobs.binary_rel FROM jobs WHERE {_JOB_PINS}"


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
        if size <= largest_offloadable:
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
                evictable = int(conn.execute(
                    "SELECT COALESCE(SUM(size), 0) " + _ELIGIBLE,
                    {"hydration": bounds.hydration_bytes, "idle_before": time.time() - eviction_idle_seconds},
                ).fetchone()[0])
            pending = _volume_bytes(vault, (row[0] for row in conn.execute(_UNPINNED_OFFLOAD, {"offload": OFFLOAD_MEDIA_TYPE})), largest)
            pinned = _volume_bytes(vault, (row[0] for row in conn.execute(_PINNED_ORIGINALS)), largest)[1]
    except _LEDGER_UNREADABLE as error:
        raise MediaStoreError("MEDIA_LEDGER_UNREADABLE", f"the media ledger cannot be read: {error}") from error
    return Occupancy(evictable, pending[0], pending[1], pinned)


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


def retained_shortfall(vault: Path, config: dict[str, object]) -> int:
    """The bytes of evicted originals that do not fit in the room below the growth threshold (D12, the retained store).

    The store drains when this reaches zero. Raises `MediaStoreError` when the policy, the ledger or the volume
    cannot give it: an unknown shortfall is never zero.
    """
    evicted, room, _total = _retained_figures(vault, config)
    return max(0, evicted - room)


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
        rows = conn.execute("SELECT binary_rel, size " + _ELIGIBLE + " " + _EVICTION_ORDER + " LIMIT :batch OFFSET :offset",
                            {**parameters, "batch": _EVICTION_BATCH, "offset": offset}).fetchall()
        return [(row["binary_rel"], int(row["size"])) for row in rows]
    finally:
        conn.close()


def _room_covers(vault: Path, ledger: MediaJobStore, parameters: dict[str, object], shortfall: int) -> bool:
    """Whether the evictable originals hold `shortfall` bytes; rows of moved originals are pruned on the way.

    The sum over the rows bounds the room from above, so a demand that it cannot cover
    reads no original.
    """
    conn = ledger._connect()
    try:
        if conn.execute("SELECT COALESCE(SUM(size), 0) " + _ELIGIBLE, parameters).fetchone()[0] < shortfall:
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
        # A retained store drains once its evicted originals fit, so a copy that fits stays (D12). An unknown
        # shortfall evicts as a served cell does: `evict` re-confirms the stored version first.
        try:
            if retained_shortfall(vault, config) == 0:
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
            if outcome == UNAVAILABLE:
                return count  # Each later candidate would wait for the same store; the next pass asks again.
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


def drain_record(vault: Path) -> DrainRecord | None:
    """The census record, or None when no census was tried; raises `MediaStoreError` when the ledger cannot be read."""
    ledger = MediaJobStore(Path(vault).resolve(), create=False)
    if not ledger.path.exists():
        return None
    try:
        with closing(ledger._connect(readonly=True)) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'media_drain'").fetchone() is None:
                return None  # A ledger from before the record has no table.
            row = conn.execute("SELECT evicted, bytes, captured_at, latched, attempted_at, attempt_failed, waiting FROM media_drain").fetchone()
    except _LEDGER_UNREADABLE as error:
        raise MediaStoreError("MEDIA_LEDGER_UNREADABLE", f"the media ledger cannot be read: {error}") from error
    if row is None:
        return None
    return DrainRecord(row["evicted"], row["bytes"], row["captured_at"], bool(row["latched"]), row["attempted_at"],
                       bool(row["attempt_failed"]), row["waiting"])


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


def _evicted_originals(vault: Path, binding: originals.StoreBinding) -> dict[str, tuple[originals.BinaryBinding, StoredObject]]:
    """The originals whose companion binds a reference to the store and whose bytes are not on the volume.

    Raises when a companion or a path cannot be read: an incomplete census is unknown, never a lower count.
    """
    found: dict[str, tuple[originals.BinaryBinding, StoredObject]] = {}
    for companion, binary, reference in media_allowance.bound_references(vault, binding):
        in_trash = trash_metadata.is_trash_path(companion)
        # A Trash companion sits beside its payload, not at the path that it binds; task 5.5 places those originals.
        if _on_volume(vault, companion.removesuffix(".md") if in_trash else binary.path):
            continue
        if in_trash:
            raise ValueError("a stored reference in Trash has no original beside it")
        found[binary.path] = (binary, reference)
    return found


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


def _released_rows(vault: Path, ledger: MediaJobStore, evicted: Iterable[str]) -> list[str]:
    """The ledger's evicted rows that the walk did not confirm, each shown to be an original that is no longer evicted.

    A row stays out of this list, and the census is unknown, when its bytes are gone and its companion is not.
    """
    counted = {ledger._relative(ledger.vault_root / path) for path in evicted}
    with closing(ledger._connect(readonly=True)) as conn:
        unconfirmed = [row[0] for row in conn.execute("SELECT binary_rel FROM media_cache WHERE evicted = 1") if row[0] not in counted]
    for relative in unconfirmed:
        if not _on_volume(vault, relative):
            _require_released(vault, relative)
    return unconfirmed


def _align_ledger(conn: sqlite3.Connection, ledger: MediaJobStore,
                  evicted: dict[str, tuple[originals.BinaryBinding, StoredObject]], released: list[str]) -> None:
    """Make the ledger's evicted rows the census: mark each counted original with its size, clear the released rows."""
    for path, (binary, reference) in evicted.items():
        conn.execute(
            "INSERT INTO media_cache(binary_rel, reference, sha256, identity, last_read, size, evicted) "
            "VALUES (?, ?, ?, '', ?, ?, 1) ON CONFLICT(binary_rel) DO UPDATE SET size = excluded.size, evicted = 1",
            (ledger._relative(ledger.vault_root / path), _reference_key(reference), binary.sha256, time.time(), binary.size))
    for relative in released:
        conn.execute("UPDATE media_cache SET evicted = 0 WHERE binary_rel = ?", (relative,))


def drain_census(vault: Path) -> tuple[int, int]:
    """Count the evicted originals from the companions, align the ledger and record the count (D12).

    The walk, the ledger write and the latch share one hold of the consistency guard, which an eviction also
    takes, so no eviction runs between the count and the latch. A complete zero latches eviction off while the
    store is turned off; a complete count above zero clears the latch. Raises `MediaStoreError` when any
    companion or path cannot be read, or when an evicted row's companion is damaged: an unknown census changes
    neither the latch nor the record.
    """
    from .writer_lease import get_manager

    vault = Path(vault).resolve()
    config, binding = originals.configuration(vault), originals.binding_for(vault)
    if config is None or binding is None:
        raise MediaStoreError("MEDIA_CENSUS_UNKNOWN", "the vault has no media store")
    ledger = MediaJobStore(vault)
    now = datetime.now(UTC)
    try:
        with get_manager().consistency_guard(vault, operation="media_drain_census", holder_kind="background"):
            evicted = _evicted_originals(vault, binding)
            released = _released_rows(vault, ledger, evicted)
            count, size = len(evicted), sum(binary.size for binary, _reference in evicted.values())
            with closing(ledger._connect()) as conn:
                conn.execute("PRAGMA synchronous=FULL")  # A latch that a power loss drops would let eviction resume.
                conn.execute("BEGIN IMMEDIATE")
                try:
                    _align_ledger(conn, ledger, evicted, released)
                    conn.execute(
                        "INSERT INTO media_drain(singleton, evicted, bytes, captured_at, latched, attempted_at) "
                        "VALUES (1, :count, :size, :at, :latched, :at) ON CONFLICT(singleton) DO UPDATE SET "
                        "evicted = :count, bytes = :size, captured_at = :at, latched = :latched, attempted_at = :at, "
                        "attempt_failed = 0",
                        {"count": count, "size": size, "at": now.isoformat(timespec="seconds"),
                         "latched": int(count == 0 and config.get("enabled") is False)})
                    conn.commit()
                except BaseException:
                    conn.rollback()
                    raise
    except (*media_allowance.UNREADABLE, sqlite3.Error) as error:
        with suppress(*_LEDGER_UNREADABLE), closing(ledger._connect()) as conn, conn:
            conn.execute("INSERT INTO media_drain(singleton, attempted_at, attempt_failed) VALUES (1, ?, 1) "
                         "ON CONFLICT(singleton) DO UPDATE SET attempted_at = excluded.attempted_at, attempt_failed = 1",
                         (now.isoformat(timespec="seconds"),))
        raise MediaStoreError("MEDIA_CENSUS_UNKNOWN", f"the companions or the ledger cannot be read ({type(error).__name__})") from error
    # The ledger commit comes first: a record that said zero before the latch held could be acted on too early.
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
    evicted, room, total = _retained_figures(vault, config)
    if evicted > room - int(margin * total):
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
            row = conn.execute("SELECT binary_rel FROM media_cache WHERE evicted = 1 AND size IS NOT NULL AND size <= ? "
                               "ORDER BY size, binary_rel LIMIT 1 OFFSET ?", (fits, skipped)).fetchone()
        if row is None:
            return hydrated, None
        if owner_read is not None and time.time() - owner_read < idle:
            return hydrated, WAIT_OWNER_READ
        if claimable() is not None:
            return hydrated, WAIT_PENDING_JOB
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
