"""Context-managed staging for consistent collection-store snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .. import held_fs, reserved_paths
from . import connection, schema, tokens

_PREFIX = ".exomem-collection-snapshot-"
_TOKEN = re.compile(r"[0-9a-f]{32}", re.ASCII)
_BUSY_TIMEOUT_MS = 50
_COMPANIONS = ("-wal", "-shm", "-journal")
# How often the copy, validation and hash loops rerun the caller's custody and fencing
# check. Its cost would otherwise grow with the store. The deadline is compared on every
# step, and every phase boundary runs the full check.
_RECHECK_SECONDS = 0.1


def _file_sha256(descriptor: int, check: Callable[[], None] | None = None) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(descriptor, 1024 * 1024):
        if check is not None:
            check()
        digest.update(chunk)
    return digest.hexdigest()


def _uuid(value: str) -> None:
    if str(uuid.UUID(value)) != value:
        raise ValueError("noncanonical UUID")


def _head(commit_seq: int, head: str | None) -> None:
    if type(commit_seq) is not int or commit_seq < 0:
        raise ValueError("invalid commit sequence")
    if commit_seq == 0:
        if head is not None:
            raise ValueError("genesis has a head")
    else:
        tokens._hex64(head, "store head")


def _validate(conn: sqlite3.Connection, check: Callable[[], None]) -> dict[str, str]:
    try:
        check()
        if conn.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise ValueError("standalone journaling unavailable")
        check()
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("integrity check failed")
        check()
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("foreign key check failed")
        check()
        metadata = dict(conn.execute("SELECT key,value FROM store_meta"))
        if metadata[schema.META_SCHEMA_VERSION] != str(schema.SCHEMA_VERSION):
            raise ValueError("unsupported schema")
        _uuid(metadata[schema.META_STORE_ID])
        _uuid(metadata[schema.META_INSTANCE_ID])
        sequence_text = metadata[schema.META_COMMIT_SEQ]
        sequence = int(sequence_text)
        if str(sequence) != sequence_text:
            raise ValueError("invalid commit sequence")
        head = metadata.get(schema.META_STORE_HEAD_HASH)
        _head(sequence, head)
        tail = conn.execute(
            "SELECT commit_seq,store_head_hash,event_hash FROM txns ORDER BY commit_seq DESC LIMIT 1"
        ).fetchone()
        if sequence == 0:
            if tail is not None:
                raise ValueError("genesis has transactions")
        else:
            if tail is None or tail[:2] != (sequence, head):
                raise ValueError("metadata disagrees with transaction tail")
            tokens._hex64(tail[2], "transaction event hash")
        lineage = json.loads(metadata[schema.META_LINEAGE])
        if not isinstance(lineage, list) or not lineage:
            raise ValueError("invalid lineage")
        current_found = False
        for entry in lineage:
            check()
            _uuid(entry["instance_id"])
            if entry["adopted_from"] is not None:
                _uuid(entry["adopted_from"])
            _head(entry["adopted_at_commit_seq"], entry["head_hash"])
            if entry["adopted_at_commit_seq"] > sequence:
                raise ValueError("lineage is ahead of the store")
            current_found |= entry["instance_id"] == metadata[schema.META_INSTANCE_ID]
        if not current_found:
            raise ValueError("current instance is absent from lineage")
        return metadata
    except (KeyError, TypeError, ValueError, AttributeError, sqlite3.DatabaseError) as error:
        raise connection.CollectionStoreError(
            "COLLECTION_SNAPSHOT_INVALID", "copied store failed snapshot validation"
        ) from error


@dataclass(frozen=True, slots=True)
class SnapshotArtifact:
    path: Path
    size_bytes: int
    file_sha256: str
    schema_version: int
    store_id: str
    instance_id: str
    commit_seq: int
    head_hash: str | None
    lineage_json: str
    lease_epoch: str | None


@contextmanager
def staged_snapshot(
    vault_root: Path,
    *,
    directory: Path,
    deadline: float,
    cancelled: Callable[[], bool] | None = None,
    scratch_token: str | None = None,
) -> Iterator[SnapshotArtifact]:
    """Yield a closed, fsynced copy in the caller's existing private directory.

    This trusted internal API authorizes only its unique scratch family. The
    caller owns staging-directory access and any later destination publication.
    ``deadline`` is an absolute monotonic cooperative limit; OS I/O can block.
    ``cancelled`` (the caller's custody and fencing check) may raise its own error.
    It runs at every phase boundary, and at most every ``_RECHECK_SECONDS`` within a
    phase. A passed deadline raises ``TimeoutError`` (COLLECTION_SNAPSHOT_DEADLINE),
    never COLLECTION_SNAPSHOT_INVALID.
    The calling worker thread owns the source reader and closes it after backup.
    """
    checked_at = 0.0

    def check() -> None:
        nonlocal checked_at
        if time.monotonic() >= deadline:
            raise TimeoutError("COLLECTION_SNAPSHOT_DEADLINE: snapshot staging deadline elapsed")
        if cancelled is not None and cancelled():
            raise connection.CollectionStoreError(
                "COLLECTION_SNAPSHOT_CANCELLED", "snapshot staging was cancelled"
            )
        checked_at = time.monotonic()

    def paced_check() -> None:
        now = time.monotonic()
        if now >= deadline:
            raise TimeoutError("COLLECTION_SNAPSHOT_DEADLINE: snapshot staging deadline elapsed")
        if now - checked_at >= _RECHECK_SECONDS:
            check()

    check()
    if scratch_token is not None and (
        not isinstance(scratch_token, str) or not _TOKEN.fullmatch(scratch_token)
    ):
        raise ValueError("invalid snapshot scratch token")
    directory = Path(directory).resolve(strict=True)
    path = directory / f"{_PREFIX}{scratch_token or secrets.token_hex(16)}.sqlite"
    owned_file = None
    owns_companions = False
    cleanup_digest = None
    with held_fs.acquire(directory).require() as filesystem:
        with filesystem.parent(".").require() as parent:
            try:
                check()
                with reserved_paths._subsystem_authority_scope("collection_store.replica"):
                    # Retain the original inode so an unlinked name cannot reuse it.
                    # Read access permits SQLite's separate writer on Windows.
                    owned_file = filesystem.file(
                        parent, path.name, create=True, exclusive=True
                    ).require()
                    identity = owned_file.identity
                for suffix in _COMPANIONS:
                    existing = filesystem.file(parent, path.name + suffix)
                    if existing.ok:
                        existing.require().close()
                        raise connection.CollectionStoreError(
                            "COLLECTION_SNAPSHOT_INVALID", "snapshot scratch family already exists"
                        )
                    if existing.error.code != "MISSING":
                        raise existing.error
                owns_companions = True
                check()
                with closing(
                    sqlite3.connect(path, isolation_level=None, timeout=_BUSY_TIMEOUT_MS / 1000)
                ) as destination:
                    check()
                    with closing(
                        connection.open_reader(
                            connection.store_path(vault_root), busy_timeout_ms=_BUSY_TIMEOUT_MS
                        )
                    ) as source:
                        check()
                        source.execute("BEGIN")
                        source.execute("SELECT key,value FROM store_meta").fetchall()
                        check()
                        source.backup(
                            destination,
                            pages=64,
                            sleep=0.01,
                            progress=lambda _status, _remaining, _total: paced_check(),
                        )
                    check()
                    # A check that stops the SQL (deadline, cancellation, custody,
                    # fencing) is re-raised as itself, whatever error type the caller's
                    # check uses; it says nothing about the copy.
                    interruption = None

                    def validation_progress() -> int:
                        nonlocal interruption
                        try:
                            paced_check()
                        except BaseException as error:  # noqa: BLE001 - SQLite would discard it
                            interruption = error
                            return 1
                        return 0

                    destination.set_progress_handler(validation_progress, 1000)
                    try:
                        metadata = _validate(destination, check)
                    except connection.CollectionStoreError as error:
                        if interruption is not None:
                            raise interruption from error.__cause__
                        raise
                    finally:
                        destination.set_progress_handler(None, 0)
                check()
                if any(path.with_name(path.name + suffix).exists() for suffix in _COMPANIONS):
                    raise connection.CollectionStoreError(
                        "COLLECTION_SNAPSHOT_INVALID", "snapshot requires SQLite companions"
                    )
                owns_companions = False
                with filesystem.file(parent, path.name, access="write").require() as file:
                    if file.identity != identity:
                        raise connection.CollectionStoreError(
                            "COLLECTION_SNAPSHOT_INVALID", "snapshot file identity changed"
                        )
                    descriptor = file.descriptor
                    digest = _file_sha256(descriptor, paced_check)
                    size = os.fstat(descriptor).st_size
                    check()
                    os.fsync(descriptor)
                check()
                cleanup_digest = digest
                yield SnapshotArtifact(
                    path=path,
                    size_bytes=size,
                    file_sha256=digest,
                    schema_version=int(metadata[schema.META_SCHEMA_VERSION]),
                    store_id=metadata[schema.META_STORE_ID],
                    instance_id=metadata[schema.META_INSTANCE_ID],
                    commit_seq=int(metadata[schema.META_COMMIT_SEQ]),
                    head_hash=metadata.get(schema.META_STORE_HEAD_HASH),
                    lineage_json=metadata[schema.META_LINEAGE],
                    lease_epoch=metadata.get(schema.META_LEASE_EPOCH),
                )
            finally:
                try:
                    if owned_file is not None:
                        for suffix in ("", *(_COMPANIONS if owns_companions else ())):
                            current = filesystem.file(parent, path.name + suffix, access="mutate")
                            if current.error is not None and current.error.code == "MISSING":
                                continue
                            if cleanup_digest is not None and current.error is not None:
                                continue
                            with current.require() as file:
                                if suffix == "" and not held_fs._same_file_identity(
                                    file.identity, identity
                                ):
                                    continue
                                if suffix == "" and cleanup_digest is not None:
                                    # Publication can fail while the original
                                    # inode now contains foreign input. Retain it.
                                    try:
                                        if _file_sha256(file.descriptor) != cleanup_digest:
                                            continue
                                    except OSError:
                                        continue
                                filesystem.unlink(file).require()
                finally:
                    if owned_file is not None:
                        owned_file.close()
