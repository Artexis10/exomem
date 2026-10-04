"""Private isolated create/restart producer; production admission remains closed.

One intent references the immutable create receipt. It survives cutover in shared
snapshots without requiring a new business transaction or replica publication.
"""

from __future__ import annotations

import json
import mmap
import os
import secrets
import sqlite3
import tempfile
import time
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

from .. import held_fs, state_migration, state_paths, vault, writer_lease
from .. import structured_collections as collections
from ..cli_ops import OpError
from . import authority, chain, connection, replica, schema
from .connection import CollectionStoreError


@contextmanager
def _isolated_session(parent):
    """Allocate custody; only this live owner (or its forked child) can resume it."""
    with ExitStack() as stack:
        session = _IsolatedSession(parent, stack)
        try:
            state_migration.require_vault_state_ready(session.root)
            session.state_fs = stack.enter_context(held_fs.acquire(session.path.parent.parent).require())
            session.state_directory = stack.enter_context(session.state_fs.parent(session.path.parent.name).require())
            yield session
        finally:
            session.alive[0] = 0


class _IsolatedSession:
    def __init__(self, parent, stack):
        parent = Path(parent).resolve()
        if not state_paths.state_store_root().resolve().is_relative_to(parent):
            raise CollectionStoreError("COLLECTION_STORE_CUSTODY_REQUIRED", "state must be inside the isolated parent")
        self.root = Path(tempfile.mkdtemp(prefix="collection-create-", dir=parent))
        (self.root / vault.kb_dirname()).mkdir()
        self.fs = stack.enter_context(held_fs.acquire(parent).require())
        self.directory = stack.enter_context(self.fs.parent(self.root.name).require())
        self.kb_directory = stack.enter_context(self.fs.parent(
            (self.root / vault.kb_dirname()).relative_to(parent).as_posix(),
        ).require())
        self.namespace = stack.enter_context(self.fs.parent(
            authority.marker_path(self.root).parent.relative_to(parent).as_posix(), create=True, access="mutate",
        ).require())
        self.alive = stack.enter_context(mmap.mmap(-1, 1))
        self.alive[0] = 1
        self.owner = os.getpid()
        self.path = connection.store_path(self.root).resolve()
        self.manager = None

    def check(self):
        try:
            if (self.alive[0] != 1 or (os.getpid() != self.owner and os.getppid() != self.owner)
                    or self.path != connection.store_path(self.root).resolve()):
                return False
            self.fs.validate_directory(self.directory).require()
            self.fs.validate_directory(self.kb_directory).require()
            self.fs.validate_directory(self.namespace).require()
            self.state_fs.validate_directory(self.state_directory).require()
            return True
        except (OSError, ValueError, held_fs.HeldFsError):
            return False

    def bind(self, manager):
        if type(self) is not _IsolatedSession:
            raise state_migration.StateMigrationOfflineRequired("isolated collection-store custody is absent")
        if not self.check() or not manager.config.enabled:
            raise CollectionStoreError("COLLECTION_STORE_CUSTODY_REQUIRED", "isolated writer custody is absent")
        if self.manager is not None and self.manager is not manager:
            raise CollectionStoreError("COLLECTION_STORE_CUSTODY_REQUIRED", "session already has a writer")
        self.manager = manager

    def runtime(self):
        from .runtime import CollectionStoreRuntime

        runtime = self.manager._collection_store
        if runtime is None:
            runtime = CollectionStoreRuntime(self.root, self.manager, authority_check=self.check,
                                             _bootstrap=True)
        if runtime.root != self.root or not runtime._bootstrap:
            raise CollectionStoreError("COLLECTION_STORE_CUSTODY_REQUIRED", "foreign runtime binding")
        with self.manager._report_lock:
            with self.manager._lock:
                if self.manager._store_borrowers or self.manager._store_handoff:
                    raise CollectionStoreError("COLLECTION_STORE_BUSY", "runtime is still borrowed")
                if runtime._handle is not None:
                    runtime._handle.close()
                    runtime._handle = None
                    runtime._writer_token = None
        return runtime

    def require(self, token):
        if not self.check():
            raise CollectionStoreError("COLLECTION_STORE_CUSTODY_REQUIRED", "isolated directory custody was lost")
        self.manager.validate_fencing_token(token)
        return self.manager._mutation_coordinator_for(self.root).current_thread_holds_boundary()

    @contextmanager
    def writer(self, token):
        from .writer import CollectionWriter

        self.require(token)
        manager_context = writer_lease._ACTIVE_LEASE_MANAGER.set(self.manager)
        fence_context = writer_lease._ACTIVE_WRITE_FENCE.set((self.manager, token))
        try:
            with connection.open_writer(self.path, lease_check=lambda: self.require(token)) as handle:
                yield CollectionWriter(self.root, handle)
        finally:
            writer_lease._ACTIVE_WRITE_FENCE.reset(fence_context)
            writer_lease._ACTIVE_LEASE_MANAGER.reset(manager_context)


def create_new(session, manager, manifest_path, manifest_text, *, why, request_id,
               fence_client, scaffold=True):
    _IsolatedSession.bind(session, manager)
    with manager.consistency_guard(session.root, operation="collection_store_create"):
        if session.path.exists():
            session.runtime()
        with manager.writer_authority_guard(vault_root=session.root):
            token = manager._fencing_token
            state_migration._require_collection_store_recovery(session, token)
            with session.writer(manager._fencing_token) as writer:
                with writer._authorization(), writer.read_snapshot():
                    _, _, replay = writer._identity(
                        "create", manifest_path,
                        {"manifest_text": manifest_text, "why": why, "scaffold": scaffold}, request_id,
                    )
                    if replay is not None:
                        result = _replay_create(writer, request_id, replay)
                        if authority.pending_create(writer.connection) is None:
                            runtime = manager._collection_store
                            marker = authority.parse_marker(session.root, authority.read_marker(session.root))
                            _verify_acquisition(writer, runtime, fence_client.collection_store_fence(), marker)
                if replay is not None and authority.pending_create(writer.connection) is None:
                    # Only an already-admitted token can skip durable epoch publication.
                    if runtime._admitted_token != token:
                        published = _publish_current_epoch(session, writer, token)
                        if published.status != "published":
                            return {**result, "status": "pending", "reason": published.reason or published.status}
                    session.require(token)
                    return result
                with manager._report_lock:
                    try:
                        if replay is not None:
                            prepared = _recover_create(writer)
                        else:
                            prepared = _prepare_new(writer, manifest_path, manifest_text, why=why,
                                                    request_id=request_id, scaffold=scaffold)
                    finally:
                        # A rejected prepare cannot revoke admission; a durable intent must gate reports.
                        runtime = manager._collection_store
                        if runtime is not None and authority.pending_create(writer.connection) is not None:
                            runtime._admitted_token = None
        if prepared["status"] == "unavailable":
            return prepared
        return _resume_locked(session, fence_client, preparation_token=token)


def _prepare_new(writer, manifest_path, manifest_text, *, why, request_id, scaffold):
    sid = writer.connection.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    expected = authority.read_marker(writer.root)
    old = None if expected is None else authority.parse_marker(writer.root, expected)
    manifest = collections.parse_manifest_bytes(writer.root, manifest_path, manifest_text.encode())
    target = {"version": 1, "mode": "store", "default_authority": "file", "store_id": sid,
              "authority_epoch": 1 if old is None else old["authority_epoch"] + 1,
              "collections": ([] if old is None else old["collections"]) + [{
                  "collection_id": manifest.collection_id, "manifest_path": manifest.path,
                  "authority": "store", "store_id": sid}],
              "collection_store_fence": {"capability": "collections-store-v1", "generation": 1}
              if old is None else old["collection_store_fence"]}
    return _prepare_create(writer, manifest_path, manifest_text, why=why, request_id=request_id,
                           scaffold=scaffold, expected_marker=expected,
                           target_marker=json.dumps(target, sort_keys=True, separators=(",", ":")).encode())


def _replay_create(writer, request_id, receipt):
    head = chain.verify_store_chain(writer.connection)
    row = writer.connection.execute(
        "SELECT t.txn_id,c.collection_id FROM txns t JOIN collections c ON c.created_txn=t.txn_id "
        "WHERE t.request_id=? AND t.operation='create' AND t.collection_id=c.collection_id",
        (request_id,),
    ).fetchone()
    if row is None:
        raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "receipt is not an immutable creation")
    writer._operation.require_collection(row[1], complete=True)
    intent = authority.pending_create(writer.connection)
    if intent is not None:
        if intent["request_id"] != request_id:
            raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "another create is pending")
        return _recover_create(writer)
    if not authority.collection_admitted(writer, row[1]) or authority.read_marker(writer.root) is None:
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "creation is no longer admitted")
    return {"status": "marker_admitted", "collection_id": row[1], "txn_id": row[0],
            "receipt": dict(receipt), "head": {"commit_seq": head[0], "head_hash": head[1]}}


def resume_local(session, manager, *, fence_client):
    _IsolatedSession.bind(session, manager)
    with manager.consistency_guard(session.root, operation="collection_store_resume"):
        return _resume_locked(session, fence_client)


def _resume_locked(session, fence_client, *, preparation_token=None):
    manager = session.manager
    if (fence_client.config.url != manager.config.url
            or fence_client.config.vault_id != manager.config.vault_id):
        raise CollectionStoreError("COLLECTION_STORE_CUSTODY_REQUIRED", "coordinator binding differs")
    runtime = session.runtime()
    with closing(connection.open_reader(session.path)) as reader:
        chain.verify_store_chain(reader)
        intent = authority.pending_create(reader)
        if intent is None:
            raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "there is no pending create")
        target = authority.parse_marker(session.root, intent["target_marker"])
        if target["store_id"] != runtime.sample_head().store_id:
            raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "create store binding differs")
    with manager._report_lock:
        runtime._admitted_token = None
    fence = fence_client.collection_store_fence()
    initial_cut = not fence.enrolled
    if not fence.enrolled:
        if intent["expected_marker"] is not None or fence.generation != 0:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "initial fence basis differs")
        if preparation_token is None:
            preparation_token = manager.ensure_writer().fencing_token
            with session.writer(preparation_token) as writer:
                recovered = _recover_create(writer)
                if recovered["status"] == "conflict":
                    return recovered
        session.require(preparation_token)
        fence = fence_client.transition_collection_store_fence(expected_generation=0, store_id=target["store_id"])
    if (fence.store_id != target["store_id"]
            or fence.generation != target["collection_store_fence"]["generation"]):
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "store fence differs")
    lease = manager.ensure_writer()
    token = lease.fencing_token
    if preparation_token is not None and not initial_cut and preparation_token != token:
        raise CollectionStoreError("COLLECTION_STORE_LEASE_REQUIRED", "preparation token was lost")
    state_migration._require_collection_store_recovery(session, token)
    with session.writer(token) as writer:
        recovered = _recover_create(writer)
        if recovered["status"] == "conflict":
            return recovered
        _verify_acquisition(writer, runtime, fence, target)
        state_migration.record_collection_store_compatibility(
            session.root, authority_check=lambda: session.require(token),
        )
        published = _publish_current_epoch(session, writer, token)
        if published.status != "published":
            return {**recovered, "status": "pending", "reason": published.reason or published.status}
        current = (published.commit_seq, published.head_hash)
        try:
            manager._renew_collection_store(token)
            _install_marker(session, token, fence_client, target, intent, current, writer)
            writer.reconcile_views()
            result = _recover_create(writer)
            if result["status"] != "marker_admitted":
                raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "marker changed after cutover")
            with writer.handle.transaction() as conn:
                session.require(token)
                if authority.marker_status(writer, intent) != "marker_admitted":
                    raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "marker changed before cleanup")
                conn.execute("DELETE FROM store_meta WHERE key=?", (authority.PENDING_CREATE,))
            return result
        except BaseException:
            with manager._report_lock:
                runtime._admitted_token = None
            raise


def _publish_current_epoch(session, writer, token):
    manager = session.manager
    deadline = time.monotonic() + 30

    def check():
        manager._renew_collection_store(token, due_only=True)
        return session.require(token)

    pending = replica.recover_replica(session.root, writer.handle, authority_check=check, deadline=deadline)
    if pending.status != "published" and (pending.reason is not None or writer.connection.execute(
        "SELECT 1 FROM store_meta WHERE key=?", (schema.META_PENDING_REPLICA_PUBLICATION,),
    ).fetchone() is not None):
        return pending
    with writer.handle.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO store_meta(key,value) VALUES (?,?)", (schema.META_LEASE_EPOCH, str(token)))
        conn.execute("INSERT OR REPLACE INTO store_meta(key,value) VALUES (?,?)", (authority.MARKER_REQUIRED, "1"))
    published = replica.publish_replica(session.root, writer.handle, authority_check=check, deadline=deadline)
    if published.status == "published":
        if chain.verify_store_chain(writer.connection) != (published.commit_seq, published.head_hash):
            raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "publication did not reach current head")
        with manager._report_lock:
            session.require(token)
            manager._collection_store._admitted_token = token
    return published


def _verify_acquisition(writer, runtime, fence, target):
    acquired = runtime._acquisition
    if (acquired is None or acquired.fencing_token != runtime.manager._fencing_token
            or fence.store_id != target["store_id"]
            or fence.generation != target["collection_store_fence"]["generation"]
            or acquired.collection_store_fence_generation != fence.generation
            or acquired.required_collection_store_capability != fence.capability):
        raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "acquisition fence differs")
    recorded = acquired.collection_store_head
    if recorded is not None:
        found = writer.connection.execute(
            "SELECT store_head_hash FROM txns WHERE commit_seq=?", (recorded.commit_seq,),
        ).fetchone() if recorded.commit_seq else (None,)
        if (recorded.store_id != target["store_id"] or found != (recorded.head_hash,)
                or recorded.instance_id != runtime.sample_head().instance_id):
            raise CollectionStoreError("COLLECTION_STORE_SYNC_PENDING", "acquired head is not in the local chain")


def _install_marker(session, token, fence_client, target, intent, current, writer):
    session.require(token)
    fence = fence_client.collection_store_fence()
    if (fence.store_id != target["store_id"]
            or fence.generation != target["collection_store_fence"]["generation"]
            or chain.verify_store_chain(writer.connection) != current):
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "cutover authority changed")
    expected = None if intent["expected_marker"] is None else intent["expected_marker"].encode()
    raw = intent["target_marker"].encode()
    if authority.read_marker(session.root) == raw:
        with session.fs.file(session.namespace, "mode.json", access="write").require() as installed:
            if session.fs.read(installed).require() != raw:
                raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "installed marker changed")
            session.require(token)
            os.fsync(installed.descriptor)
        session.fs.flush_directory(session.namespace).require()
        return
    leaf = ".exomem-collection-marker-" + secrets.token_hex(16)
    with session.fs.file(session.namespace, leaf, create=True, exclusive=True, access="write").require() as stage:
        session.fs.write(stage, raw).require()
    with session.fs.file(session.namespace, leaf, access="mutate").require() as stage:
        session.require(token)
        if authority.read_marker(session.root) != expected:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "marker preimage changed")
        session.fs.rename(stage, session.namespace, "mode.json", replace=expected is not None).require()
    session.fs.flush_directory(session.namespace).require()


class _CreateAdmission:
    def __init__(self, writer, expected_marker, target_marker):
        self.writer = writer
        self.expected = expected_marker
        self.target = target_marker
        self.txn_id = None

    def check(self, request_id, request_hash, replay):
        chain.verify_store_chain(self.writer.connection)
        if not isinstance(request_id, str) or not request_id:
            raise collections.CollectionError("INVALID_REQUEST_ID", "deferred create needs a request identity")
        intent = authority.pending_create(self.writer.connection)
        if intent is not None:
            if (intent["request_id"] != request_id or intent["request_hash"] != request_hash
                    or intent["expected_marker"] != (None if self.expected is None else self.expected.decode())
                    or intent["target_marker"] != self.target.decode() or replay is None):
                raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "another create intent or marker basis is pending")
            self.txn_id = intent["txn_id"]
        elif replay is not None:
            raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "create receipt lacks its admission intent")

    def record(self, manifest, txn):
        writer = self.writer
        writer._publication.deferred_create = manifest.collection_id
        self.txn_id = txn["txn_id"]
        target = authority.parse_marker(writer.root, self.target)
        sid = writer._publication.identity["store_id"]
        entry = {"collection_id": manifest.collection_id, "manifest_path": manifest.path,
                 "authority": "store", "store_id": sid}
        old = None if self.expected is None else authority.parse_marker(writer.root, self.expected)
        if (target["store_id"] != sid or target["collections"] !=
                ([] if old is None else old["collections"]) + [entry]
                or target["authority_epoch"] != (1 if old is None else old["authority_epoch"] + 1)
                or (old is not None and (old["store_id"] != sid or
                    old["collection_store_fence"] != target["collection_store_fence"]))
                or authority.read_marker(writer.root) != self.expected):
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "create marker basis differs")
        intent = {"version": 1, "request_id": txn["request_id"], "request_hash": txn["request_hash"],
                  "collection_id": manifest.collection_id, "manifest_path": manifest.path,
                  "source_path": manifest.storage.source, "txn_id": txn["txn_id"],
                  "expected_marker": None if self.expected is None else self.expected.decode(),
                  "target_marker": self.target.decode()}
        writer._execute("INSERT INTO store_meta(key,value) VALUES (?,?)",
                        (authority.PENDING_CREATE, json.dumps(intent, sort_keys=True, separators=(",", ":"))))


def _prepare_create(writer, manifest_path, manifest_text, *, why, request_id,
                    expected_marker, target_marker, scaffold=True):
    admission = _CreateAdmission(writer, expected_marker, target_marker)
    receipt = writer._create_collection(manifest_path, manifest_text, why=why, request_id=request_id,
                                        scaffold=scaffold, admission=admission)
    try:
        return _recover_create(writer)
    except (CollectionStoreError, collections.CollectionError, chain.StoreChainError, sqlite3.Error, OpError) as error:
        return {"status": "unavailable", "collection_id": receipt["collection_id"],
                "txn_id": admission.txn_id, "receipt": dict(receipt), "head": None,
                "reason": getattr(error, "code", "COLLECTION_STORE_UNAVAILABLE")}


def _recover_create(writer):
    writer.handle.require_write_authority()
    with writer._authorization(), writer.read_snapshot():
        intent = authority.pending_create(writer.connection)
        if intent is None:
            return None
        writer._operation.require_collection(intent["collection_id"], complete=True)
        head = chain.verify_store_chain(writer.connection)
        row = writer.connection.execute(
            "SELECT t.receipt_json FROM txns t JOIN collections c ON c.collection_id=t.collection_id "
            "WHERE t.txn_id=? AND t.operation='create' AND t.request_id=? AND t.request_hash=? "
            "AND c.collection_id=? AND c.manifest_path=? AND c.source_path=? AND c.created_txn=t.txn_id",
            (intent["txn_id"], intent["request_id"], intent["request_hash"], intent["collection_id"],
             intent["manifest_path"], intent["source_path"]),
        ).fetchone()
        if row is None:
            raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "create intent lacks its committed transaction")
        return {"status": authority.marker_status(writer, intent), "collection_id": intent["collection_id"],
                "txn_id": intent["txn_id"], "receipt": json.loads(row[0]),
                "head": {"commit_seq": head[0], "head_hash": head[1]}}
