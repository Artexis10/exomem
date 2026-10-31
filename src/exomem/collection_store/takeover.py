"""Lease takeover and copied-vault adoption of the shared replica (design §16 A3-A5).

A replica becomes the live store only after its copied bytes pass snapshot and
chain validation, its identity matches the authority marker and coordinator,
it contains every marker collection, it carries the recorded head and its
lineage includes the instance that reported that head. A lagging replica is
``COLLECTION_STORE_SYNC_PENDING``; a fork is ``COLLECTION_STORE_DIVERGED``.
Adoption copies one store: file collections are never read or imported. When
this host's store already carries the replica's head, its tenure is recorded in
place instead, and adoption keeps only the newest superseded store.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import ExitStack, closing, contextmanager, nullcontext
from dataclasses import dataclass, field

from .. import held_fs
from . import authority, chain, connection, legacy_originals, replica, schema, snapshot

RECHECK_SECONDS = 10.0
ATTENTION_SECONDS = 15 * 60.0
REMEDY = ("wait for vault sync to deliver Knowledge Base/_Collections/collections.sqlite, or "
          "continue from this host's copy with adopt-local: maintain_memory(mode=\"collections-store-adopt-local\") "
          "while the service runs, `exomem collections adopt-local` once it is stopped")
SYNC_PENDING = "COLLECTION_STORE_SYNC_PENDING"
DIVERGED = "COLLECTION_STORE_DIVERGED"
# Transient: the store was borrowed or changed while the takeover settled; rechecked next use.
BUSY = "COLLECTION_STORE_BUSY"
# Single-host custody stopped verifying: publication and writes wait, reads continue.
CUSTODY_LOST = "COLLECTION_STORE_CUSTODY_UNVERIFIED"
# The writer's migration of a store an earlier release wrote failed and rolled back; retried when due.
SCHEMA_PENDING = "COLLECTION_STORE_SCHEMA_PENDING"
# An unfinished reverse export owns the store: no host serves it until the owner finishes it.
EXPORTING = "COLLECTION_STORE_EXPORT_PENDING"
SUPERSEDED = ".superseded-"
_DROPPED = (schema.META_PUBLISHED_REPLICA_HEAD, schema.META_PUBLISHED_REPLICA_EPOCH,
            schema.META_PENDING_REPLICA_PUBLICATION, authority.PENDING_CREATE)


def _signature(found):
    return found.st_ino, found.st_size, found.st_mtime_ns


def replica_signature(root):
    try:
        return _signature(os.stat(replica.replica_path(root)))
    except FileNotFoundError:
        return None


@dataclass(frozen=True, slots=True)
class Unchanged:
    """What a same-head re-admission decided on: the published bookkeeping and the replica it names."""

    evidence: tuple
    witness: tuple


def replica_witness(root):
    """The vault replica's full path signature, change time included, or None when it is absent."""
    from ..vault_portability import _source_signature

    try:
        return _source_signature(os.lstat(replica.replica_path(root)))
    except FileNotFoundError:
        return None


def unchanged_replica(session, recorded, local):
    """Evidence that the vault replica already carries this instance's recorded head, else None.

    It holds when the coordinator recorded this instance's live head, the store published
    that head under its current lease epoch with no pending intent, and the vault replica
    holds exactly the bytes the store last published. Hashing runs here, outside the
    mutation boundary; settlement compares the same evidence cheaply under it.
    """
    from ..vault_portability import (
        PortabilityError,
        _cross_source_signature,
        _open_regular_source_native,
        _source_signature,
    )

    if (recorded is None or local is None or recorded.instance_id != local.instance_id
            or (recorded.commit_seq, recorded.head_hash) != (local.commit_seq, local.head_hash)
            # A store that settlement migrates republishes its replica at the new schema.
            or connection.schema_pending(session.path)):
        return None
    with closing(connection.open_head_reader(session.path)) as reader:
        evidence = replica.published_evidence(legacy_originals.metadata(reader))
    if evidence is None:
        return None
    source = replica.replica_path(session.root)
    try:
        descriptor, before = _open_regular_source_native(source)
        with os.fdopen(descriptor, "rb") as shared:
            digest = hashlib.file_digest(shared, "sha256").hexdigest()
            after = os.fstat(shared.fileno())
        found = source.lstat()
        if _source_signature(after) != before or _cross_source_signature(found) != _cross_source_signature(after):
            return None
    except (OSError, PortabilityError):
        # An absent, replaced or unsafe replica is the full publication path's to judge.
        return None
    return Unchanged(evidence, _source_signature(found)) if digest == evidence[1] else None


@dataclass
class Pending:
    """An unresolved takeover for one lease token: reads continue, writes refuse."""

    token: int
    code: str
    reason: str
    recorded: int | None
    local: int | None
    signature: tuple | None
    since: float = field(default_factory=time.monotonic)
    checked: float = field(default_factory=time.monotonic)

    @property
    def attention(self):
        return time.monotonic() - self.since >= ATTENTION_SECONDS

    def due(self, root):
        return (self.code == BUSY or time.monotonic() - self.checked >= RECHECK_SECONDS
                or replica_signature(root) != self.signature)

    def status(self):
        return {"status": self.code.removeprefix("COLLECTION_STORE_").lower(), "code": self.code,
                "reason": self.reason, "attention": self.attention,
                "recorded_commit_seq": self.recorded, "local_commit_seq": self.local}

    def error(self):
        remedy = {SYNC_PENDING: REMEDY, BUSY: "reads continue; the takeover is rechecked on next use",
                  CUSTODY_LOST: "reads continue; collection writes and publication resume once "
                                "single-host custody verifies again",
                  SCHEMA_PENDING: "the store keeps its earlier schema, and the migration is retried on the next "
                                  f"use at least {RECHECK_SECONDS:g} s later"}
        message = (f"{self.reason}; recorded commit_seq {self.recorded}, local commit_seq {self.local}; "
                   + remedy.get(self.code, "reads continue; reconcile before collection writes resume"))
        if self.attention:
            message = "attention: unresolved for 15 minutes; " + message
        return connection.CollectionStoreError(self.code, message)


@dataclass
class Refused:
    """A settlement refusal for one lease token, raised again until its basis changes or it is due.

    Unlike Pending it admits no reads: the original refusal stands. It keeps only the code and
    reason, so a repeated refusal carries no earlier traceback.
    """

    token: int
    code: str
    reason: str
    signature: tuple | None
    marker: bytes | None
    checked: float = field(default_factory=time.monotonic)

    def due(self, root):
        return (time.monotonic() - self.checked >= RECHECK_SECONDS
                or replica_signature(root) != self.signature or authority.read_marker(root) != self.marker)

    def error(self):
        return connection.CollectionStoreError(self.code, self.reason)


@dataclass
class Staged:
    leaf: str
    digest: str
    # The copied replica's (inode, size, mtime), or None if it changed while being copied.
    signature: tuple | None
    metadata: dict
    collections: dict
    relation: dict
    lineage: list
    canonical: dict | None
    reader: sqlite3.Connection | None = field(repr=False)

    def close_reader(self):
        if self.reader is not None:
            self.reader.close()
            self.reader = None


@contextmanager
def staged_replica(session, raw_check, heads=()):
    """Retain one private validated copy through routing and the takeover decision."""
    from .. import structured_collections as collections
    from ..cli_ops import OpError
    from ..vault_portability import (
        PortabilityError,
        _cross_source_signature,
        _open_regular_source_native,
        _source_signature,
        _validate_collection_replica_connection,
    )

    leaf = f".exomem-adopt-{secrets.token_hex(16)}.sqlite"
    path = session.path.parent / leaf
    source = replica.replica_path(session.root)
    digest = hashlib.sha256()
    signature = None
    conn = None
    held = ExitStack()
    stopped = None

    def check():
        nonlocal stopped
        try:
            raw_check()
        except BaseException as error:
            stopped = error
            raise

    try:
        try:
            # A published replica has no SQLite companions; unknown families never supply recovery facts.
            if any(os.path.lexists(str(source) + suffix) for suffix in snapshot._COMPANIONS):
                raise connection.CollectionStoreError(SYNC_PENDING, "the replica requires SQLite companions")
            try:
                descriptor, before = _open_regular_source_native(source)
            except FileNotFoundError:
                descriptor = None
            if descriptor is None:
                result = None
            else:
                with os.fdopen(descriptor, "rb") as shared:
                    staged = held.enter_context(session.state_fs.file(
                        session.state_directory, leaf, create=True, exclusive=True, access="write").require())
                    while chunk := shared.read(1024 * 1024):
                        check()
                        digest.update(chunk)
                        remaining = memoryview(chunk)
                        while remaining:
                            remaining = remaining[os.write(staged.descriptor, remaining):]
                    os.fsync(staged.descriptor)
                    after = os.fstat(shared.fileno())
                    if (_source_signature(after) != before
                            or _cross_source_signature(source.lstat()) != _cross_source_signature(after)):
                        raise connection.CollectionStoreError(BUSY, "the replica changed during staging")
                    signature = _signature(after)
                check()
                # Earlier schemas keep their established before/after migration validation in this same copy.
                snapshot.migrate_copy(path, check)
                conn = sqlite3.connect(path, isolation_level=None)
                held.callback(conn.close)
                with connection.checked_sql(conn, check):
                    metadata, canonical = _validate_collection_replica_connection(session.root, conn, check)
                    result = Staged(
                        leaf, digest.hexdigest(), signature, metadata,
                        {entry["collection_id"]: entry["manifest_path"] for entry in canonical["collections"]},
                        {head: chain.head_relation(conn, *head) for head in heads if head is not None},
                        json.loads(metadata[schema.META_LINEAGE]), canonical, conn,
                    )
        except (connection.CollectionStoreError, chain.StoreChainError, PortabilityError,
                collections.CollectionError, OpError) as error:
            if stopped is not None:
                raise stopped from None
            # These fixed validation codes describe candidate bytes, never authority or native I/O failures.
            invalid = (isinstance(error, chain.StoreChainError)
                       or isinstance(error, collections.CollectionError)
                       or isinstance(error, connection.CollectionStoreError) and error.code in {
                           "COLLECTION_SNAPSHOT_INVALID", "COLLECTION_STORE_MARKER_CONFLICT",
                           "COLLECTION_STORE_CREATE_CONFLICT", SYNC_PENDING}
                       or isinstance(error, PortabilityError) and error.code in {
                           "INVALID_ARCHIVE", "UNSAFE_SYMLINK", "UNSAFE_SOURCE_ENTRY"}
                       or isinstance(error, OpError) and error.code == "COLLECTION_STORE_UNAVAILABLE")
            if not invalid:
                if isinstance(error, PortabilityError) and error.code == "SOURCE_CHANGED_DURING_EXPORT":
                    raise connection.CollectionStoreError(BUSY, "the replica changed while it was opened") from error
                raise
            result = Staged(leaf, digest.hexdigest(), signature, {}, {}, {}, [], None, conn)
        yield result
    finally:
        held.close()  # Close the reader, then the held stage, before removing its old name.
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def replica_adoptability(metadata, raw_marker, *, intent_only=False):
    """Pure replica refusals shared by takeover and offline archive validation."""
    if not intent_only and schema.META_REPLICA_DIVERGENCE in metadata:
        return DIVERGED, "the vault replica records unresolved divergence"
    intent = None if metadata.get(authority.PENDING_CREATE) is None else json.loads(metadata[authority.PENDING_CREATE])
    if intent is not None and intent.get("kind") == authority.EXPORT:
        # Only the owner's export may finish the collections' departure from this store.
        return EXPORTING, "the vault replica carries an unfinished export; finish it with `exomem collections export`"
    if intent is not None and intent["target_marker"].encode() != raw_marker:
        return SYNC_PENDING, "the vault replica carries an unfinished create"
    return None


def decide(*, marker, raw_marker, recorded, local, local_relation, staged, token):
    """Return ``admit``, ``stage``, ``adopt``, ``continue`` or the unresolved code, with its reason."""
    sid = marker["store_id"]
    recorded_head = None if recorded is None else (recorded.commit_seq, recorded.head_hash)
    if recorded is not None and recorded.store_id != sid:
        return "COLLECTION_STORE_DIVERGED", "the coordinator head names another store"
    foreign = recorded is not None and (
        local is None or recorded.instance_id != local.instance_id
        or local_relation in {"ahead", "fork"})
    if local is not None and not foreign:
        if recorded is not None and local_relation not in {"same", "ancestor"}:
            return "COLLECTION_STORE_DIVERGED", "the recorded head is not in this store's chain"
        return "admit", "no foreign head was recorded"
    if staged is None:
        return "stage", "the vault replica decides this takeover"
    meta = staged.metadata
    if not meta:
        return "COLLECTION_STORE_SYNC_PENDING", "the vault replica is incomplete or invalid"
    if meta[schema.META_STORE_ID] != sid:
        return "COLLECTION_STORE_DIVERGED", "the vault replica belongs to another store"
    if schema.META_REPLICA_DIVERGENCE in meta:
        return "COLLECTION_STORE_DIVERGED", "the vault replica records unresolved divergence"
    sequence = int(meta[schema.META_COMMIT_SEQ])
    if recorded is not None:
        if sequence < recorded.commit_seq:
            return "COLLECTION_STORE_SYNC_PENDING", "the vault replica is behind the recorded head"
        if staged.relation.get(recorded_head) not in {"same", "ancestor"}:
            return "COLLECTION_STORE_DIVERGED", "the vault replica forks from the recorded head"
        if recorded.instance_id not in {entry["instance_id"] for entry in staged.lineage}:
            return "COLLECTION_STORE_DIVERGED", "the recorded holder is outside the replica lineage"
    if local is not None and staged.relation.get((local.commit_seq, local.head_hash)) not in {"same", "ancestor"}:
        return "COLLECTION_STORE_DIVERGED", "this host's store forks from the vault replica"
    epoch = meta.get(schema.META_LEASE_EPOCH)
    # Epochs compare only once this coordinator has recorded a head for the store; until
    # then (a copied vault, even after an earlier attempt enrolled its fence) they are foreign.
    if recorded is not None and epoch is not None and int(epoch) >= token:
        return "COLLECTION_STORE_DIVERGED", "the vault replica claims a lease this coordinator never granted"
    if any(staged.collections.get(entry["collection_id"]) != entry["manifest_path"]
           for entry in marker["collections"]):
        return "COLLECTION_STORE_SYNC_PENDING", "the vault replica predates the authority marker"
    refusal = replica_adoptability(meta, raw_marker, intent_only=True)
    if refusal is not None:
        return refusal
    head = (sequence, meta.get(schema.META_STORE_HEAD_HASH))
    if local is not None and recorded_head == head == (local.commit_seq, local.head_hash):
        return "continue", "this host's store already carries the recorded replica head"
    return "adopt", "the vault replica carries the recorded head"


def _restamp(conn, staged, token):
    """Write a new tenure: its instance, lineage entry and the replica bytes it continues from."""
    meta = staged.metadata
    instance = str(uuid.uuid4())
    lineage = [*staged.lineage, {
        "instance_id": instance, "adopted_from": meta[schema.META_INSTANCE_ID],
        "adopted_at_commit_seq": int(meta[schema.META_COMMIT_SEQ]),
        "head_hash": meta.get(schema.META_STORE_HEAD_HASH),
    }]
    values = {
        schema.META_INSTANCE_ID: instance,
        schema.META_LINEAGE: json.dumps(lineage, separators=(",", ":")),
        schema.META_LAST_PUBLISHED_REPLICA_SHA256: staged.digest,
        schema.META_LEASE_EPOCH: str(token),
    }
    for key, value in values.items():
        conn.execute("INSERT INTO store_meta(key,value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    for key in _DROPPED:
        conn.execute("DELETE FROM store_meta WHERE key=?", (key,))
    return meta[schema.META_STORE_ID], instance


def continue_in_place(writer, staged, token):
    """Equal heads: record this tenure in the live store instead of re-adopting identical commits."""
    with writer.handle.transaction() as conn:
        return _restamp(conn, staged, token)


def install(session, staged, token, check):
    """Make the validated copy this host's live store under a new lineage instance."""
    with closing(sqlite3.connect(session.path.parent / staged.leaf, isolation_level=None)) as conn:
        check()
        conn.execute("BEGIN IMMEDIATE")
        identity = _restamp(conn, staged, token)
        conn.execute("COMMIT")
    replace_live(session, staged.leaf, check)
    return identity


def replace_live(session, leaf, check, *, subdirectory=None, expected_identity=None, expected_version=None):
    """Rename a closed, journal-free store file into place as this host's live store.

    ``leaf`` lies in the state directory, or in its private ``subdirectory`` on the same
    volume. A live store it replaces is kept as the newest superseded store.
    """
    fs = session.state_fs
    check()
    with fs.parent(session.path.parent.name, access="mutate").require() as directory:
        if not held_fs._same_file_identity(directory.identity, session.state_directory.identity):
            raise connection.CollectionStoreError("COLLECTION_STORE_CUSTODY_REQUIRED", "state directory changed")
        with (nullcontext(directory) if subdirectory is None else fs.parent(
                f"{session.path.parent.name}/{subdirectory}", access="mutate").require()) as source, \
                fs.file(source, leaf, access="mutate").require() as adopted:
            def require_source():
                if expected_identity is not None and not held_fs._same_file_identity(adopted.identity, expected_identity):
                    raise connection.CollectionStoreError("COLLECTION_STORE_BUSY", "the sealed staging store changed")
                if expected_version is not None:
                    if adopted.version() != expected_version:
                        raise connection.CollectionStoreError("COLLECTION_STORE_BUSY", "the sealed staging bytes changed")
                    for suffix in snapshot._COMPANIONS:
                        companion = fs.file(source, leaf + suffix)
                        if companion.ok:
                            companion.require().close()
                            raise connection.CollectionStoreError("COLLECTION_STORE_BUSY", "the sealed staging store has SQLite companions")
                        if companion.error.code != "MISSING":
                            raise companion.error
            require_source()
            if session.path.exists():
                # Fold the superseded store's WAL into its own file; any open connection refuses this.
                try:
                    with closing(sqlite3.connect(session.path, isolation_level=None, timeout=0)) as live:
                        if live.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                            raise sqlite3.OperationalError("journal mode unchanged")
                except sqlite3.OperationalError as error:
                    raise connection.CollectionStoreError("COLLECTION_STORE_BUSY", "the live store is still open") from error
            for suffix in snapshot._COMPANIONS:
                if (session.path.parent / (session.path.name + suffix)).exists():
                    raise connection.CollectionStoreError("COLLECTION_STORE_BUSY", "the live store is still open")
            check()
            os.fsync(adopted.descriptor)
            require_source()
            superseded = None
            if session.path.exists():
                superseded = f"{session.path.name}{SUPERSEDED}{secrets.token_hex(8)}"
                with fs.file(directory, session.path.name, access="mutate").require() as live:
                    fs.rename(live, directory, superseded, replace=False).require()
            fs.rename(adopted, directory, session.path.name, replace=False).require()
        fs.flush_directory(directory).require()
        if superseded is not None:
            # The newest superseded store is kept for recovery; older ones never accumulate.
            older = [name for name in fs.iter_names(directory)
                     if name.startswith(session.path.name + SUPERSEDED) and name != superseded]
            for name in older:
                with fs.file(directory, name, access="mutate").require() as old:
                    fs.unlink(old).require()
            fs.flush_directory(directory).require()
