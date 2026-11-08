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
from . import authority, connection, legacy_originals, schema, tokens

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


def validated_lineage(metadata, check: Callable[[], None] = lambda: None):
    """Read the maintained lineage wire without scanning stored payloads."""
    sequence = int(metadata[schema.META_COMMIT_SEQ])
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
    return lineage


def _validate(conn: sqlite3.Connection, raw_check: Callable[[], None], *,
              version: int | None = None) -> dict[str, str]:
    """The copy's ``store_meta``, once its bytes, head and lineage check out at ``version``.

    ``version`` is the current schema unless ``migrate_copy`` checks an earlier copy first.
    """
    from .native_operands import FileRole, register_native_operands

    # An error from the caller's own check (custody, fencing, a transient read) is re-raised
    # as itself: only the copy's own validation may call the copy invalid.
    stopped: list[BaseException] = []

    def check() -> None:
        try:
            raw_check()
        except BaseException as error:
            stopped.append(error)
            raise

    try:
        check()
        # Standalone copies bind persisted index operands to their own metadata file.
        register_native_operands(conn, databases={FileRole.STORE: "main"})
        if conn.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise ValueError("standalone journaling unavailable")
        check()
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ValueError("integrity check failed")
        check()
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("foreign key check failed")
        check()
        metadata = legacy_originals.metadata(conn)
        if schema.schema_version(conn) != (schema.SCHEMA_VERSION if version is None else version):
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
        validated_lineage(metadata, check)
        return metadata
    except (KeyError, TypeError, ValueError, AttributeError, sqlite3.DatabaseError) as error:
        if stopped and error is stopped[-1]:
            raise
        raise connection.CollectionStoreError(
            "COLLECTION_SNAPSHOT_INVALID", "copied store failed snapshot validation"
        ) from error


def migrate_copy(path: Path, check: Callable[[], None] = lambda: None) -> None:
    """Bring a private copy of a store that an earlier release wrote to the current schema.

    The copy first passes ``_validate`` at its own schema, so a damaged copy refuses as
    invalid before anything migrates it. A copy at the current schema stays as it is, and the
    caller's own validation refuses a newer or unreadable one as before. The migration takes
    no writer lease and rewrites the file in place, so only a private copy may pass here.
    """
    stopped = []

    def checked():
        try:
            check()
        except BaseException as error:
            stopped.append(error)
            raise

    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        try:
            with connection.checked_sql(conn, checked):
                found = schema.schema_version(conn, ceiling=schema.SCHEMA_VERSION)
                if not 0 < found < schema.SCHEMA_VERSION:
                    return
                _validate(conn, checked, version=found)
        except (sqlite3.DatabaseError, schema.SchemaMetadataError, schema.SchemaVersionError) as error:
            if stopped and error is stopped[-1]:
                raise
            return
    try:
        with connection.staging_store(path, check=checked):
            pass  # The staging writer migrates the copy in one transaction.
    except Exception as error:  # noqa: BLE001 - a copy that cannot migrate is as unusable as a damaged one
        if stopped and error is stopped[-1]:
            raise
        raise connection.CollectionStoreError(
            "COLLECTION_SNAPSHOT_INVALID", "copied store failed its schema migration"
        ) from error


@contextmanager
def _paced(destination: sqlite3.Connection, paced_check: Callable[[], None]) -> Iterator[None]:
    """Run SQL under the caller's checks.

    A check that stops the SQL (deadline, cancellation, custody, fencing) is re-raised as itself,
    whatever error type the caller's check uses; it says nothing about the copy.
    """
    interruption = None

    def progress() -> int:
        nonlocal interruption
        try:
            paced_check()
        except BaseException as error:  # noqa: BLE001 - SQLite would discard it
            interruption = error
            return 1
        return 0

    destination.set_progress_handler(progress, 1000)
    try:
        yield
    except (connection.CollectionStoreError, sqlite3.DatabaseError) as error:
        if interruption is not None:
            raise interruption from error.__cause__
        raise
    finally:
        destination.set_progress_handler(None, 0)


def _backup_fill(source_path: Path, *, preserve_schema: bool):
    """The whole-file copy: SQLite's online backup of the store, as backup and Cloud portability need it."""

    def fill(destination: sqlite3.Connection, check: Callable[[], None], paced_check: Callable[[], None]) -> None:
        source = (connection._open_read_only(source_path, _BUSY_TIMEOUT_MS)[0]
                  if preserve_schema else connection.open_reader(source_path, busy_timeout_ms=_BUSY_TIMEOUT_MS))
        with closing(source):
            check()
            source.execute("BEGIN")
            source.execute("SELECT key FROM store_meta LIMIT 1").fetchone()
            check()
            source.backup(destination, pages=64, sleep=0.01, progress=lambda _status, _remaining, _total: paced_check())

    return fill


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _collection_fill(source_path: Path):
    """The collection copy: every table of the live store except what store mode adds to it.

    The copy is made in one read transaction over the attached live store. Each table the live store
    holds is created and, unless it is one of ``schema.VAULT_STORE_TABLES``, filled with every row. The
    lexical and graph objects that a migration copied in (named by ``store_mode_objects``) are left out
    whole. Indexes and triggers follow the rows, so no trigger fires on the copy.
    """

    def fill(destination: sqlite3.Connection, check: Callable[[], None], paced_check: Callable[[], None]) -> None:
        from .native_operands import FileRole, register_native_operands

        # An expression index reads its operands' metadata from the copy's own tables, so it binds to "main".
        register_native_operands(destination, databases={FileRole.STORE: "main"})
        destination.execute("ATTACH DATABASE ? AS live", (f"{Path(source_path).resolve().as_uri()}?mode=ro",))
        with _paced(destination, paced_check):
            destination.execute("BEGIN")
            # The first read pins one snapshot of the live store for everything below.
            found = int(destination.execute(
                "SELECT value FROM live.store_meta WHERE key = ?", (schema.META_SCHEMA_VERSION,)).fetchone()[0])
            if found != schema.SCHEMA_VERSION:
                raise connection.CollectionStoreError(
                    "COLLECTION_STORE_SCHEMA_PENDING" if found < schema.SCHEMA_VERSION else "COLLECTION_STORE_SCHEMA_NEWER",
                    "the collection store is not at the schema that this release copies")
            # A store older than the schema that adds store mode has no objects to leave out.
            left_out = {name for (name,) in destination.execute("SELECT name FROM live.store_mode_objects")
                        } if found >= schema.VAULT_STORE_SCHEMA_VERSION else set()
            # SQLite's own schema kinds (table, index, trigger, view) decide the order, so no list of them is kept.
            by_kind: dict[str, dict[str, str]] = {}
            for kind, name, owner, sql in destination.execute(
                    "SELECT type, name, tbl_name, sql FROM live.sqlite_master "
                    "WHERE sql IS NOT NULL AND substr(name, 1, 7) <> 'sqlite_'").fetchall():
                if name not in left_out and owner not in left_out:
                    by_kind.setdefault(kind, {})[name] = sql
            tables, indexes = by_kind.pop("table", {}), by_kind.pop("index", {})
            # A virtual table's shadow tables and module state are not rows to copy, so refuse the copy.
            if any(name in tables for (name,) in destination.execute(
                    "SELECT name FROM pragma_table_list WHERE schema = 'live' AND type = 'virtual'")):
                raise connection.CollectionStoreError(
                    "COLLECTION_SNAPSHOT_INVALID", "the collection store holds a virtual table that store mode does not own")
            for sql in tables.values():
                destination.execute(sql)
            check()
            # The schema fixes this set: store mode's tables stay in the copy, empty.
            copied = [name for name in tables if name not in schema.VAULT_STORE_TABLES]
            for name in copied:
                columns = destination.execute(
                    "SELECT name, type, pk FROM pragma_table_xinfo(?, 'live') WHERE hidden = 0 ORDER BY cid", (name,)).fetchall()
                names = [_quoted(column) for column, _type, _pk in columns]
                without_rowid = destination.execute(
                    "SELECT wr FROM pragma_table_list WHERE schema = 'live' AND name = ?", (name,)).fetchone()[0]
                keyed = [column for column in columns if column[2]]
                # A rowid table keeps its rowids, which an INTEGER PRIMARY KEY column already carries.
                if not without_rowid and not (len(keyed) == 1 and keyed[0][1].upper() == "INTEGER"):
                    names.insert(0, "rowid")
                listed = ", ".join(names)
                destination.execute(f"INSERT INTO main.{_quoted(name)}({listed}) SELECT {listed} FROM live.{_quoted(name)}")
                check()
            for sql in indexes.values():
                destination.execute(sql)
            for sqls in by_kind.values():
                for sql in sqls.values():
                    destination.execute(sql)
            destination.execute(f"PRAGMA main.user_version = {int(destination.execute('PRAGMA live.user_version').fetchone()[0])}")
            destination.execute("COMMIT")
        # Integrity checks cover every attached database, and the copy alone must stand.
        destination.execute("DETACH DATABASE live")

    return fill


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
    pending_create_raw: str | None


@contextmanager
def staged_snapshot(
    vault_root: Path,
    *,
    directory: Path,
    deadline: float,
    cancelled: Callable[[], bool] | None = None,
    scratch_token: str | None = None,
    cleanup: bool = True,
) -> Iterator[SnapshotArtifact]:
    """Stage the live vault store, requiring its current schema as before."""
    with staged_store_snapshot(
        connection.store_path(vault_root), directory=directory, deadline=deadline,
        cancelled=cancelled, scratch_token=scratch_token, cleanup=cleanup,
    ) as artifact:
        yield artifact


@contextmanager
def staged_store_snapshot(
    source_path: Path,
    *,
    directory: Path,
    deadline: float,
    cancelled: Callable[[], bool] | None = None,
    scratch_token: str | None = None,
    preserve_schema: bool = False,
    cleanup: bool = True,
) -> Iterator[SnapshotArtifact]:
    """Yield a closed, fsynced whole-file copy of the store in the caller's existing private directory."""
    with _staged_copy(
        _backup_fill(source_path, preserve_schema=preserve_schema), directory=directory, deadline=deadline,
        cancelled=cancelled, scratch_token=scratch_token, preserve_schema=preserve_schema, cleanup=cleanup,
    ) as artifact:
        yield artifact


@contextmanager
def staged_collection_snapshot(
    vault_root: Path,
    *,
    directory: Path,
    deadline: float,
    cancelled: Callable[[], bool] | None = None,
    scratch_token: str | None = None,
    cleanup: bool = True,
) -> Iterator[SnapshotArtifact]:
    """Stage the live vault store as the collection replica: its collection data without store mode's tables.

    The copy has the live store's schema, so it adopts as a store of that schema. It carries every
    collection row and none of the pages, lexical rows or graph rows that a store-mode vault keeps.
    In file mode those tables are empty, so the copy holds the same data as ``staged_snapshot``.
    """
    with _staged_copy(
        _collection_fill(connection.store_path(vault_root)), directory=directory, deadline=deadline,
        cancelled=cancelled, scratch_token=scratch_token, cleanup=cleanup, attaches_source=True,
    ) as artifact:
        yield artifact


@contextmanager
def _staged_copy(
    fill: Callable[[sqlite3.Connection, Callable[[], None], Callable[[], None]], None],
    *,
    directory: Path,
    deadline: float,
    cancelled: Callable[[], bool] | None = None,
    scratch_token: str | None = None,
    preserve_schema: bool = False,
    cleanup: bool = True,
    attaches_source: bool = False,
) -> Iterator[SnapshotArtifact]:
    """Yield a closed, fsynced copy in the caller's existing private directory.

    ``fill(destination, check, paced_check)`` writes the scratch database; this stages, validates and
    fsyncs whatever it wrote. This trusted internal API authorizes only its unique scratch family. The
    caller owns staging-directory access and any later destination publication.
    ``deadline`` is an absolute monotonic cooperative limit; OS I/O can block.
    ``cancelled`` (the caller's custody and fencing check) may raise its own error.
    It runs at every phase boundary, and at most every ``_RECHECK_SECONDS`` within a
    phase. A passed deadline raises ``TimeoutError`` (COLLECTION_SNAPSHOT_DEADLINE),
    never COLLECTION_SNAPSHOT_INVALID.
    The calling worker thread owns the source reader and closes it after backup.
    ``attaches_source`` opens the scratch database as a URI, so that the fill can attach the source read-only.
    Publication passes ``cleanup=False`` because its durable intent owns scratch;
    context exit then closes handles and leaves retirement to witnessed recovery.
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
                with closing(sqlite3.connect(
                    path.as_uri() if attaches_source else path, uri=attaches_source,
                    isolation_level=None, timeout=_BUSY_TIMEOUT_MS / 1000,
                )) as destination:
                    check()
                    fill(destination, check, paced_check)
                    check()
                    with _paced(destination, paced_check):
                        version = schema.schema_version(destination) if preserve_schema else None
                        metadata = _validate(destination, check, version=version)
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
                    pending_create_raw=metadata.get(authority.PENDING_CREATE),
                )
            finally:
                try:
                    if owned_file is not None and cleanup:
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
