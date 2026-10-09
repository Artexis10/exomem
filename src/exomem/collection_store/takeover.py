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
from contextlib import closing, contextmanager
from dataclasses import dataclass, field

from .. import held_fs
from . import authority, chain, connection, replica, schema, snapshot

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
SUPERSEDED = ".superseded-"
_DROPPED = (schema.META_PUBLISHED_REPLICA_HEAD, schema.META_PENDING_REPLICA_PUBLICATION, authority.PENDING_CREATE)


def _signature(found):
    return found.st_ino, found.st_size, found.st_mtime_ns


def replica_signature(root):
    try:
        return _signature(os.stat(replica.replica_path(root)))
    except FileNotFoundError:
        return None


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
                                "single-host custody verifies again"}
        message = (f"{self.reason}; recorded commit_seq {self.recorded}, local commit_seq {self.local}; "
                   + remedy.get(self.code, "reads continue; reconcile before collection writes resume"))
        if self.attention:
            message = "attention: unresolved for 15 minutes; " + message
        return connection.CollectionStoreError(self.code, message)


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


@contextmanager
def staged_replica(session, check, heads):
    """Copy the shared replica into private state custody and validate exactly those bytes."""
    leaf = f".exomem-adopt-{secrets.token_hex(16)}.sqlite"
    path = session.path.parent / leaf
    digest = hashlib.sha256()
    try:
        try:
            shared = open(replica.replica_path(session.root), "rb")  # noqa: SIM115 - read-only copy
        except FileNotFoundError:
            yield None
            return
        with shared, session.state_fs.file(session.state_directory, leaf, create=True, exclusive=True,
                                           access="write").require() as staged:
            before = _signature(os.fstat(shared.fileno()))
            while chunk := shared.read(1024 * 1024):
                check()
                digest.update(chunk)
                os.write(staged.descriptor, chunk)
            os.fsync(staged.descriptor)
            signature = before if _signature(os.fstat(shared.fileno())) == before else None
        conn = sqlite3.connect(path, isolation_level=None)
        try:
            metadata = snapshot._validate(conn, check)
            chain.verify_store_chain(conn)
            marker = authority.parse_marker(session.root, authority.read_marker(session.root))
            collections = dict(conn.execute("SELECT collection_id,manifest_path FROM collections"))
            # Preserve the takeover decision's foreign-store and lagging-replica verdicts before checking owned paths.
            if metadata[schema.META_STORE_ID] == marker["store_id"] and all(
                    collections.get(entry["collection_id"]) == entry["manifest_path"] for entry in marker["collections"]):
                authority.require_marker(conn, marker, root=session.root)
            result = Staged(
                leaf, digest.hexdigest(), signature, metadata, collections,
                {head: chain.head_relation(conn, *head) for head in heads if head is not None},
                json.loads(metadata[schema.META_LINEAGE]),
            )
        except (connection.CollectionStoreError, chain.StoreChainError, sqlite3.DatabaseError):
            result = Staged(leaf, digest.hexdigest(), signature, {}, {}, {}, [])
        finally:
            conn.close()
        yield result
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


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
    intent = meta.get(authority.PENDING_CREATE)
    if intent is not None and json.loads(intent)["target_marker"].encode() != raw_marker:
        return "COLLECTION_STORE_SYNC_PENDING", "the vault replica carries an unfinished create"
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
    if session.path.exists():
        # Fold the superseded store's WAL into its own file; any open connection refuses this.
        try:
            with closing(sqlite3.connect(session.path, isolation_level=None, timeout=0)) as live:
                if live.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                    raise sqlite3.OperationalError("journal mode unchanged")
        except sqlite3.OperationalError as error:
            raise connection.CollectionStoreError("COLLECTION_STORE_BUSY", "the live store is still open") from error
    for suffix in ("-wal", "-shm", "-journal"):
        if (session.path.parent / (session.path.name + suffix)).exists():
            raise connection.CollectionStoreError("COLLECTION_STORE_BUSY", "the live store is still open")
    fs = session.state_fs
    check()
    with fs.parent(session.path.parent.name, access="mutate").require() as directory:
        if not held_fs._same_file_identity(directory.identity, session.state_directory.identity):
            raise connection.CollectionStoreError("COLLECTION_STORE_CUSTODY_REQUIRED", "state directory changed")
        superseded = None
        if session.path.exists():
            superseded = f"{session.path.name}{SUPERSEDED}{secrets.token_hex(8)}"
            with fs.file(directory, session.path.name, access="mutate").require() as live:
                fs.rename(live, directory, superseded, replace=False).require()
        with fs.file(directory, staged.leaf, access="mutate").require() as adopted:
            os.fsync(adopted.descriptor)
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
    return identity
