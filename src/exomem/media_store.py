"""Encrypted, version-pinned originals; the volume is a verified local cache.

The filesystem adapter requires explicit test configuration. No store is selected
by default. Jobs share the media ledger; protection records outlive those jobs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

from . import held_fs, originals, reserved_paths
from .media_jobs import OFFLOAD_MEDIA_TYPE, MediaJob, MediaJobStore, pid_alive
from .originals import OriginalSnapshot, StoredObject, original_state


class MediaStoreError(RuntimeError):
    def __init__(self, code: str, reason: str):
        self.code = code
        super().__init__(f"{code}: {reason}")


@dataclass(frozen=True, slots=True)
class ObjectHead:
    size: int
    sha256: str


class Store(Protocol):
    def upload(self, name: str, stream: BinaryIO) -> str: ...
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


class FilesystemStore:
    """Versioned ciphertext store for isolated tests, with no deletion API."""

    def __init__(self, root: Path):
        self.root = root

    def _path(self, name: str, version: str) -> str:
        if not originals.is_sha256(name) or not version or Path(version).name != version or version in {".", ".."}:
            raise ValueError("invalid object locator")
        return f"{name}/{version}"

    def upload(self, name: str, stream: BinaryIO) -> str:
        version = uuid.uuid4().hex
        self._path(name, version)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with held_fs.acquire(self.root).require() as filesystem:
            with filesystem.parent(name, create=True, access="mutate").require() as parent:
                held_fs.publish_bytes(filesystem, parent, version, held_fs.Chunks(lambda: _chunks(stream))).require()
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
    if config.get("adapter") != "filesystem-test":
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the media store adapter is unavailable")
    return FilesystemStore(Path(str(config["root"])))


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
    conn.execute(
        "INSERT INTO media_cache(binary_rel, reference, sha256, identity, last_read) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(binary_rel) DO UPDATE SET reference=excluded.reference, sha256=excluded.sha256, "
        "identity=excluded.identity, last_read=excluded.last_read",
        (store._relative(snapshot.path), _reference_key(snapshot.reference), snapshot.binary_sha256,
         _source_identity(source), time.time()),
    )


def _clear_dead_access(conn) -> None:
    for row in conn.execute("SELECT token, pid FROM media_access").fetchall():
        if not pid_alive(row["pid"]):
            conn.execute("DELETE FROM media_access WHERE token = ?", (row["token"],))


@contextmanager
def _pin(vault: Path, path: Path):
    store = MediaJobStore(vault)
    token = uuid.uuid4().hex
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _clear_dead_access(conn)
        conn.execute("INSERT INTO media_access(token, binary_rel, pid) VALUES (?, ?, ?)",
                     (token, store._relative(path), os.getpid()))
        conn.commit()
    finally:
        conn.close()
    try:
        yield store, token
    finally:
        conn = store._connect()
        try:
            with conn:
                conn.execute("UPDATE media_cache SET last_read = ? WHERE binary_rel = ?",
                             (time.time(), store._relative(path)))
                conn.execute("DELETE FROM media_access WHERE token = ?", (token,))
        finally:
            conn.close()


def _reserve(store: MediaJobStore, token: str, snapshot: OriginalSnapshot, config: dict[str, object]) -> None:
    assert snapshot.reference is not None and snapshot.binary_size is not None
    needed = snapshot.binary_size + snapshot.reference.ciphertext_size
    maximum = int(config["max_temporary_bytes"])
    concurrent = int(config["max_hydrations"])
    if maximum <= 0 or concurrent <= 0:
        raise ValueError("media hydration limits must be positive")
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        _clear_dead_access(conn)
        row = conn.execute("SELECT COALESCE(SUM(temporary_bytes), 0), COALESCE(SUM(hydrating), 0) FROM media_access").fetchone()
        if needed + row[0] > maximum or row[1] >= concurrent:
            raise MediaStoreError("MEDIA_STORE_BUSY", "hydration capacity is in use")
        conn.execute("UPDATE media_access SET temporary_bytes = ?, hydrating = 1 WHERE token = ?", (needed, token))
        conn.commit()
    finally:
        conn.close()


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
                rows = conn.execute("SELECT pid FROM media_access WHERE binary_rel = ?", (store._relative(path),)).fetchall()
                if any(pid_alive(row["pid"]) for row in rows):
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
            fields, _body, _marker = parse_frontmatter(raw.data.decode("utf-8"), strict=True)
            relative = originals.bound_path(fields)
            if not isinstance(relative, str) or sidecar != originals.sidecar_path(vault / relative):
                continue
            snapshot = original_state(vault, relative)
            if snapshot.binary_sha256 is not None:
                yield snapshot
        except (UnicodeError, ValueError):
            continue


def processing_status(vault: Path) -> list[dict[str, object]]:
    from . import media_processing
    from .vault import parse_frontmatter

    rows = []
    for snapshot in bound_originals(vault):
        if snapshot.state != "evicted":
            continue
        raw = reserved_paths.read_generic_bytes(vault, snapshot.sidecar_path.relative_to(vault).as_posix())
        text = raw.data.decode("utf-8")
        fields, _body, _marker = parse_frontmatter(text, strict=True)
        processing = fields.get("processing_state")
        media_type = fields.get("media_type")
        if processing is None and isinstance(media_type, str) and media_processing.has_completed_transcript(text, media_type=media_type):
            processing = "completed"
        rows.append({"path": snapshot.path.relative_to(vault).as_posix(),
                     # 'stored' is the processing-status state fixed by the spec.
                     "state": "stored", "original_state": snapshot.state,
                     "processing_state": processing})
    return rows


def enqueue_offload(vault: Path, path: Path) -> int:
    if originals.binding_for(vault) is None:
        return 0
    snapshot = original_state(vault, path)
    if snapshot.state != "local" or snapshot.binary_sha256 is None:
        return 0
    store = MediaJobStore(vault)
    current = store.get_by_binary(snapshot.path, media_type=OFFLOAD_MEDIA_TYPE)
    if current is not None and current.do_offload:
        return 0
    # A separate row keeps offload runnable without changing extraction verdicts.
    store.enqueue(MediaJob(snapshot.path, snapshot.sidecar_path,
                           OFFLOAD_MEDIA_TYPE,
                           do_ocr=False, do_offload=True))
    return 1


def enqueue_offloads(vault: Path) -> int:
    if originals.binding_for(vault) is None:
        return 0
    return sum(enqueue_offload(vault, item.path) for item in bound_originals(vault))


def _guard(vault: Path, operation: str):
    from .writer_lease import get_manager

    return get_manager().mutation_guard(vault, operation=operation, holder_kind="background")


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
    fields, body, _marker = parse_frontmatter(text, strict=True)
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
            conn.execute("INSERT INTO media_protections(token, binary_rel, object, created_at) VALUES (?, ?, ?, ?)",
                         (uuid.uuid4().hex, store._relative(snapshot.path), name, time.time()))
    finally:
        conn.close()


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
    finally:
        conn.close()


def offload(vault: Path, path: Path) -> StoredObject:
    """Protect, encrypt, confirm and durably publish a bound original."""
    vault = Path(vault).resolve()
    config = _settings(vault)
    if config.get("enabled") is not True:
        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "offload is disabled")
    backend = store_for(vault)
    ledger = MediaJobStore(vault)
    with _pin(vault, path):
        snapshot = original_state(vault, path)
        if snapshot.binary_sha256 is None or snapshot.binary_size is None:
            raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original has no exact sidecar binding")
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
        name_key = Path(str(config["name_key_path"])).read_bytes()
        if len(name_key) < 32:
            raise ValueError("the media name key requires at least 32 bytes")
        name = hmac.new(name_key, bytes.fromhex(snapshot.binary_sha256), hashlib.sha256).hexdigest()
        key_version = str(config["key_version"])
        reuse = next((item for item in bound_originals(vault)
                      if item.reference is not None and item.binary_sha256 == snapshot.binary_sha256
                      and item.binary_size == snapshot.binary_size and item.reference.key_version == key_version), None)
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
                _protect(ledger, snapshot, reuse.reference.object if reuse is not None else name)
            if reuse is not None:
                reference = reuse.reference
                assert reference is not None
            else:
                with tempfile.TemporaryFile() as ciphertext:
                    result = subprocess.run(["age", "--encrypt", "--recipient", str(config["recipient"])],
                                            stdin=source.stream, stdout=ciphertext, stderr=subprocess.PIPE, check=False)
                    if result.returncode:
                        raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "age encryption failed")
                    ciphertext.seek(0)
                    head = _digest(ciphertext)
                    ciphertext.seek(0)
                    version = backend.upload(name, ciphertext)
                    reference = StoredObject(1, str(config["store"]), key_version, name, version, head.size, head.sha256)
            if not _confirmed(backend, reference):
                raise MediaStoreError("MEDIA_STORE_UNAVAILABLE", "the uploaded version is not confirmed")
            with _guard(vault, "media_store_publish"):
                source.require_current()
                if original_state(vault, path) != snapshot:
                    raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the binding changed")
                _publish_reference(vault, snapshot, reference)
                _finish_reference(ledger, original_state(vault, path), source)
        return reference


def _hydrate(vault: Path, snapshot: OriginalSnapshot, ledger: MediaJobStore, token: str) -> None:
    try:
        _hydrate_reserved(vault, snapshot, ledger, token)
    finally:
        conn = ledger._connect()
        try:
            with conn:
                conn.execute("UPDATE media_access SET temporary_bytes = 0, hydrating = 0 WHERE token = ?", (token,))
        finally:
            conn.close()


def _hydrate_reserved(vault: Path, snapshot: OriginalSnapshot, ledger: MediaJobStore, token: str) -> None:
    from .vault import PathGuard, recheck_path_guards

    config = _settings(vault)
    assert snapshot.reference is not None
    _reserve(ledger, token, snapshot, config)
    backend = store_for(vault)
    guard = PathGuard.capture(vault, ledger._relative(snapshot.path), leaf_policy="absent")
    with tempfile.TemporaryFile() as ciphertext, tempfile.TemporaryFile() as plaintext:
        with backend.open(snapshot.reference.object, snapshot.reference.version) as stream:
            remaining = snapshot.reference.ciphertext_size
            while chunk := stream.read(min(1024 * 1024, remaining + 1)):
                if len(chunk) > remaining:
                    raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the ciphertext exceeds its recorded size")
                ciphertext.write(chunk)
                remaining -= len(chunk)
        ciphertext.seek(0)
        if _digest(ciphertext) != ObjectHead(snapshot.reference.ciphertext_size, snapshot.reference.ciphertext_sha256):
            raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the ciphertext size or digest differs")
        ciphertext.seek(0)
        result = subprocess.run(["age", "--decrypt", "--identity", str(config["identity_path"])],
                                stdin=ciphertext, stdout=plaintext, stderr=subprocess.PIPE, check=False)
        plaintext.seek(0)
        if result.returncode or _digest(plaintext) != ObjectHead(snapshot.binary_size, snapshot.binary_sha256):
            raise MediaStoreError("MEDIA_ORIGINAL_UNVERIFIED", "the original failed authenticated verification")
        plaintext.seek(0)
        with _guard(vault, "media_store_hydrate"):
            if original_state(vault, snapshot.path) != snapshot:
                raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the original changed during hydration")
            recheck_path_guards(vault, (guard,))
            relative = snapshot.path.relative_to(vault)
            with held_fs.acquire(vault).require() as filesystem:
                with filesystem.parent(relative.parent.as_posix(), access="mutate").require() as parent:
                    held_fs.publish_bytes(filesystem, parent, relative.name, held_fs.Chunks(lambda: _chunks(plaintext))).require()
                    filesystem.flush_directory(parent).require()
            with reserved_paths.open_generic_binary(vault, ledger._relative(snapshot.path)) as placed:
                if _digest(placed.stream) != ObjectHead(snapshot.binary_size, snapshot.binary_sha256):
                    raise MediaStoreError("MEDIA_ORIGINAL_CHANGED", "the placed original changed")
                conn = ledger._connect()
                try:
                    with conn:
                        _record_cache(conn, ledger, original_state(vault, snapshot.path), placed)
                finally:
                    conn.close()


@contextmanager
def local_originals(vault: Path, paths: tuple[str | Path, ...]):
    """Materialize selected original bindings and pin them through a local operation.

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
        if relative.lower().endswith(".md"):
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
            ledger, token = stack.enter_context(_pin(root, snapshot.path))
            current = original_state(root, snapshot.path)
            if current.state == "evicted":
                _hydrate(root, current, ledger, token)
        yield


@contextmanager
def open_original(vault: Path, path: str | Path, *, physical: bool = False):
    """Pin before resolution; retain verified bytes through the caller's read."""
    vault = Path(vault).resolve()
    target = Path(path)
    if not target.is_absolute():
        target = vault / target
    relative = target.relative_to(vault).as_posix()
    if originals.binding_for(vault) is None:
        with reserved_paths.open_generic_binary(vault, relative, physical=physical) as source:
            yield source
        return
    with _pin(vault, target) as (ledger, token):
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


def evict(vault: Path, path: Path) -> bool:
    """Confirm outside the boundary, then exclude pins/jobs through the unlink."""
    vault = Path(vault).resolve()
    config = _settings(vault)
    if config.get("enabled") is not True or config.get("eviction_enabled") is not True:
        return False
    ledger = MediaJobStore(vault)
    snapshot = original_state(vault, path)
    if snapshot.state != "cached" or snapshot.reference is None or not cache_matches(vault, path, snapshot=snapshot):
        return False
    identity = _identity(vault, path)
    try:
        confirmed = _confirmed(store_for(vault), snapshot.reference)
    except (held_fs.HeldFsError, OSError, MediaStoreError):
        return False  # An unavailable or unverified version cannot authorize removal.
    if not confirmed:
        with _guard(vault, "media_store_absent_version"):
            if original_state(vault, path) == snapshot:
                _publish_reference(vault, snapshot, None)
                enqueue_offload(vault, path)
        return False
    with _guard(vault, "media_store_evict"):
        conn = ledger._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            _clear_dead_access(conn)
            relative = ledger._relative(path)
            row = conn.execute("SELECT last_read FROM media_cache WHERE binary_rel = ?", (relative,)).fetchone()
            if (row is None or time.time() - row[0] < float(config["minimum_idle_seconds"])
                    or conn.execute("SELECT 1 FROM media_access WHERE binary_rel = ?", (relative,)).fetchone()
                    or conn.execute("SELECT 1 FROM jobs WHERE binary_rel = ?", (relative,)).fetchone()
                    or original_state(vault, path) != snapshot or _identity(vault, path) != identity):
                conn.rollback()
                return False
            # Pin admission and enqueue use this same SQLite write exclusion.
            # Retained parent/file handles prevent an exchanged ancestor redirect.
            target = path.relative_to(vault)
            with held_fs.acquire(vault).require() as filesystem:
                with filesystem.parent(target.parent.as_posix(), access="mutate").require() as parent:
                    with filesystem.file(parent, target.name, access="mutate").require() as file:
                        if _identity(vault, path) != identity:
                            conn.rollback()
                            return False
                        filesystem.unlink(file).require()
                        filesystem.flush_directory(parent).require()
            conn.commit()
            return True
        finally:
            conn.close()


def evict_to_target(vault: Path) -> int:
    config = originals.configuration(vault)
    if config is None or config.get("enabled") is not True or config.get("eviction_enabled") is not True:
        return 0
    usage = shutil.disk_usage(vault)
    target = float(config["eviction_target"])
    margin = float(config["eviction_margin"])
    if not 0 < margin < target < 1:
        raise ValueError("media eviction fractions require 0 < margin < target < 1")
    if usage.used <= usage.total * target:
        return 0
    ledger = MediaJobStore(vault)
    conn = ledger._connect(readonly=True)
    try:
        reads = {row["binary_rel"]: row["last_read"] for row in conn.execute("SELECT binary_rel, last_read FROM media_cache")}
    finally:
        conn.close()
    candidates = sorted((item for item in bound_originals(vault) if item.state == "cached"),
                        key=lambda item: (-item.binary_size, reads.get(ledger._relative(item.path), time.time())))
    count = 0
    for item in candidates:
        if shutil.disk_usage(vault).used <= usage.total * (target - margin):
            break
        count += int(evict(vault, item.path))
    return count
