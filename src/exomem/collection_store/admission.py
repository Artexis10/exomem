"""Vault-custodied create, restart, takeover and rollback for mixed-authority stores.

One intent references the immutable create receipt. It survives cutover in shared
snapshots without requiring a new business transaction or replica publication.
"""

from __future__ import annotations

import json
import logging
import mmap
import os
import secrets
import sqlite3
import tempfile
import time
from contextlib import ExitStack, closing, contextmanager, suppress
from dataclasses import replace
from pathlib import Path

from .. import capabilities, held_fs, state_migration, state_paths, vault, writer_lease
from .. import structured_collections as collections
from ..cli_ops import OpError
from . import authority, capability, chain, connection, custody, owner, replica, schema, takeover
from .connection import CollectionStoreError

logger = logging.getLogger(__name__)


@contextmanager
def _isolated_session(parent):
    """Allocate disposable custody; only this live owner (or its forked child) can resume it."""
    parent = Path(parent).resolve()
    if not state_paths.state_store_root().resolve().is_relative_to(parent):
        raise CollectionStoreError("COLLECTION_STORE_CUSTODY_REQUIRED", "state must be inside the isolated parent")
    root = Path(tempfile.mkdtemp(prefix="collection-create-", dir=parent))
    (root / vault.kb_dirname()).mkdir()
    with _ProducerSession.open(root, production=False) as session:
        yield session


@contextmanager
def production_session(vault_root):
    """Bind a real vault whose supported single-host custody verifies on its actual paths.

    Unknown custody refuses only this store producer; file collections and
    knowledge never consult it. No caller flag or attestation can mint a session.
    """
    root = Path(vault_root).resolve()
    verified = custody.verify(root)
    if not verified.verified:
        raise CollectionStoreError("COLLECTION_STORE_CUSTODY_UNVERIFIED", verified.reason)
    with _ProducerSession.open(root, production=True) as session:
        yield session


def _require_state(root):
    """Store-bearing state is validated later by token-bound recovery; a fresh copy bootstraps here."""
    recorded = state_migration.recorded_descriptor_ids(root)
    _, optional = state_migration.partition_state_descriptor_ids(recorded or ())
    marker = authority.read_marker(root)
    if marker is None and not optional:
        state_migration.require_vault_state_ready(root)
    elif recorded is None and not state_migration.bootstrap_fresh_state(root):
        raise state_migration.StateMigrationOfflineRequired("copied store vault state is not fresh")


class _ProducerSession:
    """The one private producer custody: a disposable root or a custody-verified real vault."""

    def __init__(self, root, stack, *, production):
        self.root = Path(root).resolve()
        self.production = production
        parent = self.root.parent
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
        self.fence_client = None
        # Minted only after custody verified (or on an isolated root). Re-derived at takeover,
        # create/resume, cutover and every replica publication; operations use this verdict.
        self.custody, self.custody_reason = True, "verified when the session was minted"

    @classmethod
    @contextmanager
    def open(cls, root, *, production):
        with ExitStack() as stack:
            session = cls(root, stack, production=production)
            try:
                _require_state(session.root)
                session.state_fs = stack.enter_context(held_fs.acquire(session.path.parent.parent).require())
                session.state_directory = stack.enter_context(
                    session.state_fs.parent(session.path.parent.name).require())
                yield session
            finally:
                session.alive[0] = 0

    def check(self):
        try:
            if (self.alive[0] != 1 or (os.getpid() != self.owner and os.getppid() != self.owner)
                    or self.path != connection.store_path(self.root).resolve()):
                return False
            self.fs.validate_directory(self.directory).require()
            self.fs.validate_directory(self.kb_directory).require()
            self.fs.validate_directory(self.namespace).require()
            self.state_fs.validate_directory(self.state_directory).require()
            return self.custody
        except (OSError, ValueError, held_fs.HeldFsError):
            return False

    def verify_custody(self):
        verdict = custody.verify(self.root) if self.production else custody.Custody(True, "isolated root")
        self.custody, self.custody_reason = verdict.verified, verdict.reason
        return self.custody

    def bind(self, manager):
        if type(self) is not _ProducerSession:
            raise state_migration.StateMigrationOfflineRequired("collection-store producer custody is absent")
        if not manager.config.enabled:
            raise coordination_required("the collection store's writer")
        if not self.check():
            raise CollectionStoreError("COLLECTION_STORE_CUSTODY_REQUIRED", "producer writer custody is absent")
        if self.manager is not None and self.manager is not manager:
            raise CollectionStoreError("COLLECTION_STORE_CUSTODY_REQUIRED", "session already has a writer")
        self.manager = manager

    def runtime(self, *, retire=True):
        """This session's runtime; ``retire`` closes its idle writer so a direct writer can open.

        The first live session in this process that binds a vault serves it: the export
        flush and the in-service owner route find its runtime through ``_SERVING``.
        """
        from .runtime import _SERVING, CollectionStoreRuntime

        runtime = self.manager._collection_store
        if runtime is None:
            runtime = CollectionStoreRuntime(self.root, self.manager, authority_check=self.check,
                                             _bootstrap=True)
        if runtime.root != self.root or not runtime._bootstrap:
            raise CollectionStoreError("COLLECTION_STORE_CUSTODY_REQUIRED", "foreign runtime binding")
        runtime._session = self
        serving = _SERVING.get(self.root)
        if serving is None or serving._session is None or not serving._session.check():
            _SERVING[self.root] = runtime
        if self.fence_client is not None and runtime._resolver is None:
            runtime._resolver = lambda: _resolve(self)
        runtime._publication_custody = lambda token: _publication_custody(self, token)
        if retire and not runtime.retire_idle_handle():
            raise CollectionStoreError("COLLECTION_STORE_BUSY", "runtime is still borrowed")
        return runtime

    def require(self, token):
        if not self.check():
            raise CollectionStoreError("COLLECTION_STORE_CUSTODY_REQUIRED", "producer directory custody was lost")
        self.manager.validate_fencing_token(token)
        return self.manager._mutation_coordinator_for(self.root).current_thread_holds_boundary()

    @contextmanager
    def writer(self, token):
        from .writer import CollectionWriter

        self.require(token)

        def record_schema(version):
            state_migration.record_collection_store_schema(
                self.root, version=version, authority_check=lambda: self.require(token))

        manager_context = writer_lease._ACTIVE_LEASE_MANAGER.set(self.manager)
        fence_context = writer_lease._ACTIVE_WRITE_FENCE.set((self.manager, token))
        try:
            with connection.open_writer(self.path, lease_check=lambda: self.require(token),
                                        schema_guard=record_schema) as handle:
                yield CollectionWriter(self.root, handle)
        finally:
            writer_lease._ACTIVE_WRITE_FENCE.reset(fence_context)
            writer_lease._ACTIVE_LEASE_MANAGER.reset(manager_context)


def create_new(session, manager, manifest_path, manifest_text, *, why, request_id,
               fence_client, scaffold=True):
    if session.production:
        # The first create enrols the vault in the store, irreversibly; only a release that
        # enables records-summary-v1 may do that, whatever the manifest's view mode.
        capability.require_records_summary(session.root)
        require_summary_manifest(session.root, manifest_path, manifest_text)
    session.verify_custody()
    _ProducerSession.bind(session, manager)
    _bind_fence_client(session, fence_client)
    with manager.consistency_guard(session.root, operation="collection_store_create"):
        if session.production:
            capability.require_records_summary(session.root)
            if session.path.exists():
                from . import ownership

                reader, _ = connection._open_read_only(session.path, connection.BUSY_TIMEOUT_MS)
                with closing(reader):
                    existing = {row[0] for row in reader.execute("SELECT collection_id FROM collections")}
                    intent = authority.pending_create(reader)
                    proof = ownership.resolve(session.root, conn=reader, manager=manager)
                    # First admission cannot expose unrelated preview rows; their owner must recover that private state.
                    if existing and not proof.admitted and (intent is None or existing != authority.intent_collection_ids(intent)):
                        raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "unadmitted private collections require explicit recovery")
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
                            from . import ownership

                            marker = ownership.current(session.root, conn=writer.connection).marker
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
                                                    request_id=request_id, scaffold=scaffold,
                                                    fence_client=fence_client)
                    finally:
                        # A rejected prepare cannot revoke admission; a durable intent must gate reports.
                        runtime = manager._collection_store
                        if runtime is not None and authority.pending_create(writer.connection) is not None:
                            runtime._admitted_token = None
        if prepared["status"] == "unavailable":
            return prepared
        return _resume_locked(session, fence_client, preparation_token=token)


def _prepare_new(writer, manifest_path, manifest_text, *, why, request_id, scaffold, fence_client):
    from . import ownership

    sid = writer.connection.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    expected = authority.read_marker(writer.root)
    old = None if expected is None else authority.parse_marker(writer.root, expected)
    proof = ownership.current(writer.root, conn=writer.connection)
    manifest = collections.parse_manifest_bytes(writer.root, manifest_path, manifest_text.encode())
    # Only a first enrolment reads the coordinator's fence, the basis of its cut.
    fence = fence_client.collection_store_fence() if old is None else None
    existing = list(proof.entries)
    if old is not None and proof.marker is not None:
        old = {**proof.marker, "authority_epoch": old["authority_epoch"]}
    entry = authority.marker_entry(
        manifest.collection_id, manifest.path, sid, manifest.semantic_profile, manifest.storage.source,
        manifest.storage.strategy)
    target = target_marker(writer.root, old, sid, [entry] if old is not None else [*existing, entry], fence=fence)
    authority.require_new_file_ownership(writer.root, old, target["collections"][-1])
    return _prepare_create(writer, manifest_path, manifest_text, why=why, request_id=request_id,
                           scaffold=scaffold, expected_marker=expected,
                           target_marker=json.dumps(target, sort_keys=True, separators=(",", ":")).encode())


def target_marker(root, old, sid, entries, *, fence, supersede=False):
    """The version-two marker that adds ``entries`` to ``old`` (None before first enrolment), validated.

    A first enrolment targets the generation after ``fence``, the coordinator's fence read
    before its cut: the newest retired generation, or 0 if never enrolled, so a create or a
    migration after an export re-enrols at the next one. ``supersede`` is a first enrolment
    that replaces the unadmitted store ``fence`` binds, so it too targets the next one. A
    later enrolment keeps the marker's fence and advances the authority epoch, so it
    ignores ``fence``.
    """
    if old is not None and old["version"] != 2:
        raise CollectionStoreError("COLLECTION_STORE_MARKER_UPGRADE_REQUIRED", "open the existing store before creating another collection")
    if old is None:
        # An enrolled fence makes no cut, so its basis stays the first generation as before.
        generation = fence.generation + 1 if supersede or not fence.enrolled else fence.generation
    target = {"version": 2, "mode": "store", "default_authority": "file", "store_id": sid,
              "authority_epoch": 1 if old is None else old["authority_epoch"] + 1,
              "collections": ([] if old is None else old["collections"]) + list(entries),
              "collection_store_fence": {"capability": writer_lease.COLLECTION_STORE_CAPABILITY,
                                         "generation": generation}
              if old is None else old["collection_store_fence"]}
    authority.parse_marker(root, json.dumps(target))
    return target


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
        if intent["kind"] != authority.CREATE or intent["request_id"] != request_id:
            raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "another create is pending")
        return _recover_create(writer)
    if not authority.collection_admitted(writer, row[1]):
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "creation is no longer admitted")
    return {"status": "marker_admitted", "collection_id": row[1], "txn_id": row[0],
            "receipt": dict(receipt), "head": {"commit_seq": head[0], "head_hash": head[1]}}


def resume_local(session, manager, *, fence_client):
    session.verify_custody()
    _ProducerSession.bind(session, manager)
    _bind_fence_client(session, fence_client)
    with manager.consistency_guard(session.root, operation="collection_store_resume"):
        return _resume_locked(session, fence_client)


def _bind_fence_client(session, fence_client):
    manager = session.manager
    if (fence_client.config.url != manager.config.url
            or fence_client.config.vault_id != manager.config.vault_id):
        raise CollectionStoreError("COLLECTION_STORE_CUSTODY_REQUIRED", "coordinator binding differs")
    session.fence_client = fence_client


def open_store(session, manager, *, fence_client):
    """Startup, lease takeover and copied-vault adoption for a store-routed vault.

    Returns ``admitted``, or an unresolved ``sync_pending``/``diverged``/``flush_pending``
    (or transient ``busy``) state under which reads of a local copy continue and
    collection writes refuse; ``LeaseManager.status()`` reports it with its attention
    flag. Ordinary checkout re-runs this resolution every 10 s and on replica change.
    """
    _ProducerSession.bind(session, manager)
    _bind_fence_client(session, fence_client)
    return _resolve(session, force=True)


def _resolve(session, *, force=False):
    """Admit this token's store, adopt the vault replica, or record why neither is safe yet.

    Reads of the local copy never wait on it: an unresolved verdict that is not yet due
    returns before any boundary, and the replica is copied and validated outside the
    mutation boundary. Only settling (installation, divergence and epoch publication)
    holds it, after a cheap recheck of the marker, heads and replica digest it decided on.
    """
    manager = session.manager
    runtime = session.runtime(retire=False)
    previous = runtime._takeover
    token = manager._fencing_token
    if token is not None and runtime.reporting_ready(token):
        return {"status": "admitted"}
    if not force and previous is not None and previous.token == token and not previous.due(session.root):
        return previous.status()
    if not session.verify_custody():
        return _custody_lost(session, token).status()
    with ExitStack() as stack:
        routed = _routed_store(session, stack)
        if isinstance(routed, takeover.Pending):
            with manager._report_lock:
                token = manager._fencing_token
                if token is not None and runtime._admitted_token == token:
                    return {"status": "admitted"}
                routed.token = token
                if previous is not None and previous.code == routed.code:
                    routed.since = previous.since
                runtime._takeover = routed
            return routed.status()
        raw, marker, fence, staged = routed
        with manager.writer_authority_guard(vault_root=session.root):
            token = manager._fencing_token
            if runtime.reporting_ready(token):
                return {"status": "admitted"}
            facts, diverged, signature = _takeover_facts(session, runtime, token, raw, marker, fence)
            action, reason = ((takeover.DIVERGED, "this store records unresolved divergence") if diverged
                              else takeover.decide(staged=None, **facts))
            recorded, local = facts["recorded"], facts["local"]

            def check():
                manager._renew_collection_store(token, due_only=True)
                session.require(token)

            if action == "stage":
                heads = [None if recorded is None else (recorded.commit_seq, recorded.head_hash),
                         None if local is None else (local.commit_seq, local.head_hash)]
                if staged is None:
                    staged = stack.enter_context(takeover.staged_replica(session, check, heads))
                else:
                    with connection.checked_sql(staged.reader, check):
                        staged.relation = {head: chain.head_relation(staged.reader, *head) for head in heads if head is not None}
                action, reason = ((takeover.SYNC_PENDING, "the vault replica has not arrived")
                                  if staged is None else takeover.decide(staged=staged, **facts))
            if staged is not None:
                # The same held leaf survives, but install/restamp must not retain a SQLite reader.
                staged.close_reader()
            # nosemgrep: ep-word-membership -- takeover.decide returns these action tokens.
            if action in {"admit", "adopt", "continue"} or (
                    action == takeover.DIVERGED and local is not None and not diverged):
                with manager.consistency_guard(session.root, operation="collection_store_takeover"):
                    action, reason = _settle(session, runtime, token, facts, staged, action, reason, check)
            if action == "admitted":
                return {"status": "admitted"}
            state = takeover.Pending(token, action, reason, None if recorded is None else recorded.commit_seq,
                                     None if local is None else local.commit_seq, signature)
            if previous is not None and (previous.code, previous.recorded) == (state.code, state.recorded):
                state.since = previous.since
            with manager._report_lock:
                # A racing resolve may have admitted this token meanwhile; never shadow it.
                if runtime._admitted_token == token:
                    return {"status": "admitted"}
                runtime._takeover = state
            return state.status()


def _owner_operation(session, manager, fence_client, why):
    if not isinstance(why, str) or not why.strip():
        raise ValueError("an owner store operation needs a reason")
    session.verify_custody()
    _ProducerSession.bind(session, manager)
    _bind_fence_client(session, fence_client)
    runtime = session.runtime(retire=False)
    with ExitStack() as stack:
        routed = _routed_store(session, stack)
        if isinstance(routed, takeover.Pending):
            raise routed.error()
        return runtime, routed[2]


def _stale_preview():
    return CollectionStoreError("COLLECTION_STORE_ADOPT_PREVIEW_STALE",
                                "the store, replica or recorded head changed since the preview; preview again")


def rollback_slice(session, manager):
    """Narrow S1 for this vault, preserving its canonical history and routing (S1.8).

    The mutation boundary drains any running batch before the disable commits. A
    mistaken rollback interrupts C until authorized resume; it never interrupts A/B.
    Publication remains available so backup and close can preserve the disabled store.
    """
    require_owner(session.root, "rollback")
    session.verify_custody()
    _ProducerSession.bind(session, manager)
    runtime = session.runtime(retire=False)
    with manager.writer_authority_guard(vault_root=session.root):
        token = manager._fencing_token
        with manager.consistency_guard(session.root, operation="collection_store_rollback"):
            state_migration._require_collection_store_recovery(session, token)
            if not runtime.retire_idle_handle():
                raise connection.busy("the live store is still borrowed")
            with session.writer(token) as writer:
                intent = authority.pending_create(writer.connection)
                if intent is not None and intent["kind"] == authority.EXPORT:
                    raise export_pending()
                if intent is not None:
                    # Reject unestablished custody; a false refusal delays the owner's
                    # narrowing operation, but cannot authorize a first cutover.
                    if intent["expected_marker"] is None:
                        raise CollectionStoreError("COLLECTION_STORE_LEASE_REQUIRED",
                                                   "first enrollment must finish before rollback")
                    expected = authority.parse_marker(session.root, intent["expected_marker"])
                    target = authority.parse_marker(session.root, intent["target_marker"])
                    fence = session.fence_client.collection_store_fence()
                    _require_fence(fence, expected)
                    if not fence.enrolled or runtime.sample_head().store_id != target["store_id"]:
                        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "rollback store binding differs")
                    if _acquired(runtime, fence, "COLLECTION_STORE_LEASE_REQUIRED").collection_store_head is None:
                        raise CollectionStoreError("COLLECTION_STORE_SYNC_PENDING", "rollback requires the recorded store head")
                    _verify_acquisition(writer, runtime, fence, target)
                    if _recover_create(writer)["status"] == "conflict":
                        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "create marker basis differs")
                elif not runtime.reporting_ready(token):
                    raise runtime._refusal()
                with writer.handle.transaction() as conn:
                    conn.execute("INSERT OR IGNORE INTO store_meta(key,value) VALUES (?,?)",
                                 (capability.DISABLED_KEY, "1"))
                try:
                    published = _publish_current_epoch(session, writer, token)
                    if published.status == "published":
                        manager._renew_collection_store(token)
                except (CollectionStoreError, OpError, OSError, sqlite3.Error) as error:
                    return {"status": "disabled", "capability": capability.RECORDS_SUMMARY_V1,
                            "publication": "pending", "reason": getattr(error, "code", str(error))}
    return {"status": "disabled", "capability": capability.RECORDS_SUMMARY_V1,
            "publication": published.status,
            **({"reason": published.reason} if published.reason else {})}


def rollback_route(vault_root):
    """The offline owner's narrowing-only rollback; no route can enable the slice."""
    require_owner(vault_root, "rollback")

    def apply(session, manager, fence_client):
        _ProducerSession.bind(session, manager)
        _bind_fence_client(session, fence_client)
        session.runtime(retire=False)
        with closing(connection.open_reader(session.path)) as reader:
            pending = authority.pending_create(reader)
        if pending is None:
            opened = open_store(session, manager, fence_client=fence_client)
            if opened["status"] != "admitted":
                raise session.runtime(retire=False)._refusal()
        return rollback_slice(session, manager)

    return _offline_owner(Path(vault_root).resolve(), "rollback", apply)


def adopt_local(session, manager, *, why, fence_client, preview_id=None):
    """Owner adopt-local (A3), preview-first: continue from this host's store past a foreign head.

    Without ``preview_id`` it only previews. Applying records the fork point and a new
    lineage tenure, clears the divergence it previewed, keeps the previewed foreign
    bytes as ``.foreign-*`` evidence, removes the abandoned publication's workspace and
    republishes; a changed store, replica or recorded head refuses as stale. The
    recorded head is the coordinator's fresh status, as the lease-free preview reads it.
    The runtime takes the new identity at once and re-resolves at its next use, so a
    service applying this keeps its lease and its writes. Reconcile later holds the
    other side's changes.
    """
    runtime, fence = _owner_operation(session, manager, fence_client, why)
    with manager.writer_authority_guard(vault_root=session.root):
        token = manager._fencing_token
        _acquired(runtime, fence, "COLLECTION_STORE_LEASE_REQUIRED")
        recorded = manager.client.status().collection_store_head
        with manager.consistency_guard(session.root, operation="collection_store_adopt_local"):
            sealed = owner.adopt_local_preview(session.root, recorded=recorded)
            if preview_id is None:
                return sealed
            if sealed["preview_id"] != preview_id:
                raise _stale_preview()
            if sealed["preview"]["state"] == "in_sync":
                return {"status": "in_sync", **sealed}
            published, identity, workspace = continue_past(session, runtime, token, sealed["preview"], why=why)
        if published.status != "published":
            return {"status": "pending", "reason": published.reason or published.status, **workspace, **sealed}
        manager._renew_collection_store(token)
        return {"status": "adopted", "instance_id": identity[1], **workspace, **sealed}


def continue_past(session, runtime, token, preview, *, why):
    """Adopt-local's apply under the boundary: continue this store as a new tenure past ``preview``.

    It records the fork point, keeps a foreign replica as ``.foreign-*`` evidence, removes
    the abandoned publication's workspace and republishes. Returns the publication, the
    new identity and what happened to that workspace; the caller renews once it published.
    """
    if not runtime.retire_idle_handle():
        raise CollectionStoreError(takeover.BUSY, "the live store is still borrowed")
    abandoned = preview["abandoned_publication"]
    with session.writer(token) as writer:
        with writer.handle.transaction(resolve_divergence=True) as conn:
            (predecessor,) = conn.execute("SELECT value FROM store_meta WHERE key=?", (
                schema.META_LAST_PUBLISHED_REPLICA_SHA256,)).fetchone() or (None,)
            identity = owner.record_fork(conn, preview, why=why, token=token)
        runtime.rebind(identity)
        kept = None if abandoned is None else replica.discard_abandoned_publication(
            session.root, writer.handle, abandoned, predecessor=predecessor,
            authority_check=_publication_authority(session, token), deadline=time.monotonic() + 30)
        published = _publish_current_epoch(session, writer, token)
    return published, identity, {} if abandoned is None else {"abandoned_workspace": kept or "removed"}


def reconcile_store(session, manager, *, why, fence_client, preview_id=None, acknowledge_skipped=False):
    """Owner reconcile (§15 item 5), preview-first: foreign evidence becomes held corrections.

    Every item a preserved foreign store changed after its common ancestor with this
    one is held with its values and diagnostics; no canonical row changes. A diverged
    store refuses: adopt-local decides which side continues first. Evidence with a
    change that cannot be held stays unreconciled unless the owner applies with
    ``acknowledge_skipped``, which marks every previewed file reconciled and records
    the skipped changes by id and skip code.
    """
    runtime, _fence = _owner_operation(session, manager, fence_client, why)
    with manager.writer_authority_guard(vault_root=session.root):
        token = manager._fencing_token
        if not runtime.reporting_ready(token):
            raise runtime._refusal()
        with manager.consistency_guard(session.root, operation="collection_store_reconcile"):
            with closing(connection.open_reader(session.path)) as local:
                if local.execute("SELECT 1 FROM store_meta WHERE key=?",
                                 (schema.META_REPLICA_DIVERGENCE,)).fetchone() is not None:
                    raise CollectionStoreError(takeover.DIVERGED, "run adopt-local before reconciling")
                sealed, items = owner.reconcile_plan(session.root, local)
            if preview_id is None:
                return sealed
            if sealed["preview_id"] != preview_id:
                raise _stale_preview()
            if not runtime.retire_idle_handle():
                raise CollectionStoreError(takeover.BUSY, "the live store is still borrowed")
            preview = sealed["preview"]
            acknowledged = preview["skipped"] if acknowledge_skipped else []
            reconciled = (sorted({entry["sha256"] for entry in (*preview["sources"], *acknowledged)})
                          if acknowledge_skipped else preview["reconciled"])
            with session.writer(token) as writer:
                result = writer.hold_store_delta(items, reconciled=reconciled, acknowledged=acknowledged, why=why)
    return {"status": "held", **result, **sealed}


def _coordinator_head():
    """The coordinator's recorded store head, or ``owner.UNKNOWN`` without a configured coordinator."""
    config = writer_lease.LeaseConfig.from_env()
    if not config.enabled:
        return owner.UNKNOWN
    return writer_lease.LeaseCoordinatorClient(config).status().collection_store_head


def _route_step(root):
    """Adopt-local while this store does not continue the vault's replica, then its reconcile step."""
    if not connection.store_path(root).exists():
        return "adopt-replica", owner.adopt_replica_preview(root, recorded=_coordinator_head())
    adopt = owner.adopt_local_preview(root, recorded=_coordinator_head())
    if adopt["preview"]["state"] == "in_sync":
        with closing(connection.open_reader(connection.store_path(root))) as local:
            reconcile = owner.reconcile_plan(root, local)[0]
        if reconcile["preview"]["sources"]:
            return "reconcile", reconcile
    return "adopt-local", adopt


def _serving_session(root):
    """This process's producer session serving ``root`` under a held lease, or None."""
    from .runtime import _SERVING

    runtime = _SERVING.get(root)
    session = None if runtime is None else runtime._session
    if (session is None or session.manager is not runtime.manager
            or runtime.manager._fencing_token is None or not session.check()):
        return None
    return session


def _require_no_service(config, operation):
    """Refuse an out-of-service apply while any holder's lease is live (A11, as `collections migrate`).

    Taking the lease beside a running service would hand its token back under it and
    change the store identity it serves.
    """
    record = writer_lease.LeaseCoordinatorClient(config).status()
    if record.holder is not None and record.expires_at is not None and record.expires_at > time.time():
        holder = writer_lease.holder_text(record.holder, record.expires_at)
        if record.holder.endswith(writer_lease.OFFLINE_OWNER_SUFFIX):
            raise CollectionStoreError(
                "COLLECTION_STORE_SERVICE_ACTIVE",
                f"{holder} holds this vault's writer lease; retry once it finishes or its lease expires")
        served = ("apply through its maintain_memory(mode=\"collections-store-adopt-local\") or "
                  if operation == "adopt-local" else "")
        raise CollectionStoreError(
            "COLLECTION_STORE_SERVICE_ACTIVE",
            f"a service ({holder}) holds this vault's writer lease, which expires in "
            f"{max(1, round(record.expires_at - time.time()))} s unless the service renews it; {served}stop it first")


def _apply_step(session, manager, fence_client, step, *, why, preview_id, acknowledge_skipped=False):
    if step == "adopt-replica":
        if _route_step(session.root)[1]["preview_id"] != preview_id:
            raise _stale_preview()
        _owner_operation(session, manager, fence_client, why)
        with manager.writer_authority_guard(vault_root=session.root), \
                manager.consistency_guard(session.root, operation="collection_store_adopt_replica"):
            if _route_step(session.root)[1]["preview_id"] != preview_id:
                raise _stale_preview()
            # The ordinary takeover still proves custody, marker, chain and coordinator lineage.
            return open_store(session, manager, fence_client=fence_client)
    if step == "adopt-local":
        result = adopt_local(session, manager, why=why, fence_client=fence_client, preview_id=preview_id)
        if result["status"] == "in_sync":
            # A store in sync with its replica may still be unknown to this coordinator (a copied
            # vault or a replaced coordinator): adopt-local cut its fence, so admit the store here.
            opened = open_store(session, manager, fence_client=fence_client)
            # In sync only once admitted; otherwise the store's own unresolved state is the answer.
            result = {**result, "status": "in_sync" if opened["status"] == "admitted" else opened["status"],
                      "store": opened}
        return result
    open_store(session, manager, fence_client=fence_client)
    return reconcile_store(session, manager, why=why, fence_client=fence_client, preview_id=preview_id,
                           acknowledge_skipped=acknowledge_skipped)


def adopt_local_route(vault_root, *, why=None, preview_id=None, acknowledge_skipped=False):
    """The one owner route behind `exomem collections adopt-local` and its maintain_memory mode (A3).

    Preview-first: the preview reads without the writer lease and names its step,
    adopt-local or, once this store continues, reconciling preserved foreign evidence
    into held corrections. Applying runs that step's `adopt_local` or `reconcile_store`
    with ``preview_id``. In a process that serves the vault (the service answering
    maintain_memory) it runs on that service's own session and lease, which it keeps.
    Elsewhere (the CLI) it refuses while any service holds the lease; otherwise it runs
    in its own producer session under the configured lease and hands the lease back
    with the flushed head. ``acknowledge_skipped`` applies the reconcile step's owner
    acknowledgement of changes it cannot hold.
    """
    require_owner(vault_root, "adopt-local")
    root = Path(vault_root).resolve()
    # The apply would refuse at the store; the preview says so instead of reporting a sync state.
    _require_no_pending_migration(root)
    step, sealed = _route_step(root)
    if acknowledge_skipped and step != "reconcile":
        if sealed["preview"]["state"] == "in_sync":
            # Nothing awaits reconciliation, as after an acknowledgement that already applied.
            raise CollectionStoreError(
                "COLLECTION_STORE_ACKNOWLEDGE_UNAVAILABLE",
                "no preserved evidence awaits reconciliation, so there is nothing to acknowledge; "
                "an earlier acknowledgement may already have applied")
        raise CollectionStoreError("COLLECTION_STORE_ACKNOWLEDGE_UNAVAILABLE",
                                   f"the acknowledgement applies to the reconcile step; the next step is {step}")
    if preview_id is None:
        if sealed["preview"].get("recorded_head") == owner.UNKNOWN:
            return {"step": step, **sealed, "warnings": [
                "no writer lease is configured here, so the coordinator's head was not read; "
                "an apply needs a preview taken with the configured writer lease"]}
        return {"step": step, **sealed}
    serving = _serving_session(root)
    if serving is not None:
        # In the service: its own session and writer credential, never the operator's.
        fence_client = serving.fence_client or serving.manager.client
        if fence_client is None:
            raise coordination_required("adopt-local")
        return {"step": step, **_apply_step(serving, serving.manager, fence_client, step, why=why,
                                            preview_id=preview_id, acknowledge_skipped=acknowledge_skipped)}
    from .runtime import _SERVERS, served

    if served(root):
        # This process serves the vault but its store is not open: answer as its routes do,
        # never with the offline step that needs the operator credential.
        server = _SERVERS.get(root)
        code, message = server.refusal if server is not None else (
            "COLLECTION_STORE_UNAVAILABLE", "the collection store is opening")
        raise CollectionStoreError(code, message)
    return {"step": step, **_offline_owner(root, "adopt-local", lambda session, manager, fence_client: _apply_step(
        session, manager, fence_client, step, why=why, preview_id=preview_id,
        acknowledge_skipped=acknowledge_skipped))}


def create_route(vault_root, manifest_path, manifest_text, *, why, request_id):
    """The owner's offline create behind `exomem collections create` (S1.8 enrolment).

    It runs with the service stopped, in its own producer session under the configured
    lease and the operator credential, so it may make the coordinator's first store fence
    cut that enrols the vault; on an enrolled vault it adds the collection as the
    service's route does. It hands the lease back with the flushed head.
    """
    require_owner(vault_root, "create")
    capability.require_records_summary(vault_root)  # before any lease, fence or operator credential
    require_summary_manifest(Path(vault_root).resolve(), manifest_path, manifest_text)
    return _offline_owner(Path(vault_root).resolve(), "create", lambda session, manager, fence_client: create_new(
        session, manager, manifest_path, manifest_text, why=why, request_id=request_id,
        fence_client=fence_client))


def migrate_route(vault_root, *, step, deadline=None):
    """The owner's route behind `exomem collections migrate` (P1b.3b, design §10 and A11).

    ``dry-run`` is the read-only preflight. ``prepare`` pre-imports and proves into private
    staging and may run beside the service. ``apply`` runs with the service stopped, in its
    own producer session under the configured lease and the operator credential, as the
    offline create does, so it may make the coordinator's first store fence cut; it hands
    the lease back with the flushed head. ``handoff`` is the same apply run by a managed
    upgrade's migrator after the old worker stopped, under its self-imposed ``deadline``
    (absolute monotonic), which abandons what it has not published by then (P1b.3).
    """
    require_owner(vault_root, "migrate")
    capability.require_collections_migrate()  # before any lease, fence or operator credential
    from . import migration

    root = Path(vault_root).resolve()
    if step == "dry-run":
        return migration.dry_run(root)
    if step == "prepare":
        return migration.prepare(root)
    # nosemgrep: ep-word-membership -- the closed steps of `collections migrate` and the managed migrator.
    if step in ("apply", "handoff"):
        if (step == "handoff") != (deadline is not None):
            raise ValueError("a handoff's migration needs its deadline, and only a handoff has one")

        def work(session, manager, fence_client):
            # The final source proof can outlast one lease; the offline owner stops renewal after handoff.
            manager.start_renewer()
            return migration.cutover(session, manager, fence_client, deadline=deadline)

        result = _offline_owner(root, "migrate", work)
        if result.get("status") == migration.RESTORED and "lease" not in result:
            # The restore handed its lease back with the head it published; the migration
            # starts afresh under a new acquisition, which records that head.
            return {**_offline_owner(root, "migrate", work), "restored": result["restored"]}
        return result
    raise ValueError("migrate step must be dry-run, prepare, apply or handoff")


def export_route(vault_root, *, plan_id=None, why=None, retire_local=False, abandon=False):
    """The owner's route behind `exomem collections export` and its maintain_memory mode (P1b.4).

    Without ``plan_id`` it previews, read-only and without the lease. Applying runs only
    offline: with the service stopped, in its own producer session under the
    configured lease and the operator credential, as the offline migrate does, so it may
    retire the coordinator's store fence. A process that serves the vault refuses it.
    ``abandon`` gives up the unfinished export ``plan_id`` names instead (P1b.4a), offline
    in the same way. ``retire_local`` is the step for every other host once the exported
    vault reached it.
    """
    require_owner(vault_root, "export")
    from . import export

    root = Path(vault_root).resolve()
    if retire_local:
        # Reading the fence needs only the writer credential, and the step changes this host alone.
        return _offline_owner(root, "export", export.retire_local, operator=False)
    if plan_id is None:
        if abandon:
            raise ValueError("abandoning an export needs its plan_id")
        return export.preview(root)
    from .runtime import served

    if served(root):
        # Retiring the fence revokes the lease this service writes under.
        raise CollectionStoreError(
            "COLLECTION_STORE_SERVICE_ACTIVE",
            "an export applies only offline: stop the service, then run `exomem collections export --plan-id ID "
            "--why REASON` with EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN set")
    step = export.abandon if abandon else export.apply
    # Abandon queues view recovery, so its closing owner must not publish those views.
    return _offline_owner(root, "export", lambda session, manager, fence_client: step(
        session, manager, fence_client, plan_id=plan_id, why=why), drain_views=not abandon)


def require_summary_manifest(root, manifest_path, manifest_text):
    """Refuse a production store create whose manifest is not a summary collection (S1).

    Items mode stays in files under this slice. A manifest that does not parse passes
    on, so the create's own validation names what is wrong with it.
    """
    from .summary import SUMMARY

    try:
        manifest = collections.parse_manifest_bytes(root, manifest_path, manifest_text.encode())
    except ValueError:  # CollectionError included
        return
    if manifest.view_mode != SUMMARY:
        raise CollectionStoreError(
            "COLLECTION_STORE_SUMMARY_REQUIRED",
            "the collection store creates only summary collections: add \"view_mode: summary\" to the "
            "manifest, or create an items-mode collection with "
            f"{capabilities.active_call_text('record_memory', {'action': 'create'})}, "
            "which keeps it in files")


ENROLLMENT_COMMAND = ("exomem collections create --manifest-path PATH --manifest-file FILE --why REASON, "
                      "with the service stopped and EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN set")


ADOPTION_COMMAND = ("exomem collections adopt-local --why REASON --dry-run, then again with --preview-id ID "
                    "instead of --dry-run, with the service stopped and EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN set")


def migration_pending():
    """The refusal for a store an unfinished migration owns: only the owner's apply finishes it."""
    return CollectionStoreError(
        "COLLECTION_STORE_LEASE_REQUIRED",
        "an unfinished migration owns this store; finish it with `exomem collections migrate --apply`")


def export_pending():
    """The refusal for a store an unfinished reverse export owns: only the owner's export finishes it."""
    return CollectionStoreError(
        takeover.EXPORTING,
        "an unfinished export to files owns this store; finish it with `exomem collections export --plan-id ID "
        "--why REASON`, with the service stopped and EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN set")


def _require_no_pending_migration(root):
    """Refuse an owner step on a store an unfinished migration or export owns, at its preview as at its apply."""
    path = connection.store_path(root)
    if path.exists():
        with closing(connection.open_reader(path)) as reader:
            intent = authority.pending_create(reader)
        if intent is not None and intent["kind"] == authority.MIGRATE:
            raise migration_pending()
        if intent is not None and intent["kind"] == authority.EXPORT:
            raise export_pending()


def adoption_required():
    """The refusal for a routed vault whose coordinator never enrolled its store (a copy or a new coordinator)."""
    return CollectionStoreError(
        "COLLECTION_STORE_ADOPTION_REQUIRED",
        "this coordinator never enrolled this vault's collection store, as when the vault was copied or its "
        f"coordinator replaced; continuing it is the owner's offline step: run {ADOPTION_COMMAND}")


def unenrolled(fence):
    """The refusal for a routed vault whose coordinator holds no store fence: never enrolled, or retired."""
    if fence.generation == 0:
        return adoption_required()
    return CollectionStoreError(
        "COLLECTION_STORE_RETIRED",
        f"the coordinator retired this vault's collection store at fence generation {fence.generation}, so "
        "the store marker on this host is stale; this host serves the vault again once its export to files "
        "reaches this host")


COORDINATION_REQUIRED = "COLLECTION_STORE_COORDINATION_REQUIRED"
COORDINATION_SETUP = ("configure the single-host writer-lease coordinator, as docs/deployment.md describes under "
                      "\"Automatic single-writer lease and laptop takeover\"")


def coordination_required(operation):
    """The refusal for a store step on an install that configures no writer-lease coordinator.

    Store mode requires lease coordination (ruled D7): the capability fence, the head
    exchange and takeover all live in the coordinator. This prevents an install from
    reaching a store that no fence protects, or a step that refuses for an unrelated
    reason; a false refusal costs the owner a rerun with the service's lease environment.
    """
    from .. import cloud_cell

    if cloud_cell.cloud_mode_enabled():
        # A Cloud cell's environment cannot carry a lease URL (cellctl forbids the prefix),
        # so the owner-side remedy below would only mislead its tenant.
        return CollectionStoreError(
            COORDINATION_REQUIRED, f"{operation} needs writer-lease coordination, which Cloud cells do not "
            "provide yet; summary collections and the collection store are not available on this cell")
    # It names what this process observes: a CLI run from a shell without the service's
    # EnvironmentFile sees no lease URL even where the service has a coordinator.
    return CollectionStoreError(
        COORDINATION_REQUIRED, f"{operation} needs writer-lease coordination, and EXOMEM_WRITER_LEASE_URL is not "
        "set in this process's environment; run it with the service's lease environment, or, where no "
        f"coordinator exists, {COORDINATION_SETUP}")


def enrollment_required():
    """The refusal for a summary create that needs the coordinator's operator fence cut."""
    if not writer_lease.LeaseConfig.from_env().enabled:
        return coordination_required("enrolling this vault's collection store")
    return CollectionStoreError(
        "COLLECTION_STORE_ENROLLMENT_REQUIRED",
        f"enrolling this vault's collection store is the owner's offline step: run {ENROLLMENT_COMMAND}")


def require_owner(root, operation):
    from ..governance import raw_protection
    from ..governance.principal import effective_principal

    who = effective_principal()
    # RAW exempts hosted tenants from owner-only reads; that exemption grants no maintenance role.
    if not (raw_protection.is_owner(who) and raw_protection.has_unrestricted_access(root, who)):
        raise CollectionStoreError("COLLECTION_STORE_OWNER_REQUIRED", f"{operation} is owner-only")


def _offline_owner(root, operation, work, *, operator=True, drain_views=True):
    """Run ``work(session, manager, fence_client)`` as the owner's offline store step.

    It refuses while any service holds the lease, then holds its own producer session and
    lease, which it hands back with the flushed head. ``operator=False`` is for a step
    that only reads the fence: its client carries the writer credential, not the operator's.
    """
    config = writer_lease.LeaseConfig.from_env()
    fence_client = (writer_lease.configured_schema_fence_operator_client() if operator
                    else writer_lease.LeaseCoordinatorClient(config) if config.enabled else None)
    if fence_client is None:
        raise coordination_required(operation)
    _require_no_service(config, operation)
    # Its own holder, so the coordinator arbitrates against an idle-released service that
    # re-acquires mid-step rather than handing both processes one token.
    # This finite owner acquires only for its step and keeps that grant until close.
    config = replace(config, replica_id=config.replica_id + writer_lease.OFFLINE_OWNER_SUFFIX,
                     preferred_writer=False, idle_release_seconds=0)
    with production_session(root) as session:
        manager = writer_lease.LeaseManager(config)
        close_options = {} if drain_views else {"drain_views": False}
        try:
            try:
                result = work(session, manager, fence_client)
            except BaseException:
                # The refusal is the answer; an unadmitted token keeps its lease until it expires.
                with suppress(Exception):
                    manager.close(**close_options)
                raise
            try:
                manager.close(**close_options)
            except OpError as error:
                if operation == "migrate" and result.get("status") != "marker_admitted":
                    # A migrate that admitted nothing has no head to hand off; the report must not hide.
                    result = {**result, "lease": "expires", "lease_reason": error.code}
                elif operation != "rollback" or result["status"] != "disabled":
                    raise
                else:
                    # The local disable already committed; a failed handoff must not hide it.
                    result = {**result, "publication": "pending", "reason": error.code}
        finally:
            # Close finishes its flush before renewal stops and the producer session releases custody.
            manager._stop.set()
            renewer = manager._renewer
            if renewer is not None and renewer.is_alive():
                renewer.join()
    return result


def _custody_lost(session, token):
    """Unverified custody leaves C pending with attention: reads continue, writes and publication wait."""
    runtime = session.manager._collection_store
    previous = runtime._takeover
    state = takeover.Pending(token, takeover.CUSTODY_LOST, session.custody_reason, None,
                             runtime.sample_head().commit_seq if session.path.exists() else None,
                             takeover.replica_signature(session.root))
    if previous is not None and previous.code == state.code:
        state.since = previous.since
    runtime._takeover = state
    return state


def _publication_custody(session, token):
    """Re-derive custody once per replica publication, before it writes into the vault."""
    if session.verify_custody():
        return True
    _custody_lost(session, token)
    return False


def _migrate_schema(session, token):
    """Migrate a store an earlier release wrote once admission's checks pass; the failure's reason, or None.

    Admission settles under the writer lease and the mutation boundary, after its fence
    cut and recovery check, which read only the store's head. So it never migrates a store
    that it then refuses. The writer records the new schema's IDs before it migrates, and
    the migration keeps the store's rows and head, so the decision it settles still holds.
    """
    if not connection.schema_pending(session.path):
        return None
    try:
        with session.writer(token):
            pass  # open_writer migrates the schema in its own transaction.
    except Exception as error:  # noqa: BLE001 - any failure that left the earlier schema is this pending state
        if not connection.schema_pending(session.path):
            raise  # The migration committed; what failed after it is not a pending schema.
        logger.warning("the collection store schema migration did not commit; it is retried", exc_info=True)
        return ("the store schema migration did not commit, so the store keeps its earlier schema: "
                f"{type(error).__name__}: {error}")
    return None


def _routed_store(session, stack):
    """The marker this takeover serves, with the coordinator's store fence bound to it.

    A copied vault meeting a fresh coordinator cuts the fence over from its marker,
    which revokes any earlier token, so this precedes the lease acquisition. Its
    structural declarations do not establish public file authority or store admission.
    """
    from . import ownership

    def pending(code, reason):
        # Only known candidate/transition evidence becomes Pending; caller and native I/O failures still raise.
        return takeover.Pending(session.manager._fencing_token, code, reason, None, None,
                                takeover.replica_signature(session.root))

    _, enrolled = ownership.enrollment(session.root)
    local = session.path.exists()
    marker = staged = None
    if local:
        reader, _ = connection._open_read_only(session.path, connection.BUSY_TIMEOUT_MS)
        with closing(reader):
            reader.execute("BEGIN")
            intent = authority.pending_create(reader)
            if intent is not None:
                from .runtime import served

                if (intent["kind"] == authority.CREATE and served(session.root)
                        and authority.intent_status(session.root, reader, intent) == "pending"):
                    # The serving store thread resumes it, so the same request succeeds later.
                    raise connection.busy("a collection create is still publishing; retry shortly")
                if intent["kind"] == authority.MIGRATE:
                    return pending(takeover.SYNC_PENDING, migration_pending().reason)
                if intent["kind"] == authority.EXPORT:
                    return pending(takeover.EXPORTING, export_pending().reason)
                # A marker that changed under the create never resolves by retrying.
                raise CollectionStoreError("COLLECTION_STORE_LEASE_REQUIRED",
                                           "create recovery owns this store; resume that create")
            if enrolled:
                # Recovery needs namespace facts before takeover can satisfy a public reader's head floor.
                current_fence = session.fence_client.collection_store_fence()
                marker, _ = ownership.declarations(session.root, reader, generation=max(1, current_fence.generation))
    raw = authority.read_marker(session.root)
    if enrolled and local:
        if marker is None:
            raise CollectionStoreError("COLLECTION_STORE_UNAVAILABLE", "no admitted collection store remains")
    else:
        def check():
            if not session.check():
                raise CollectionStoreError("COLLECTION_STORE_CUSTODY_REQUIRED", "producer directory custody was lost")
            token = session.manager._fencing_token
            if token is not None:
                session.manager._renew_collection_store(token, due_only=True)
                session.require(token)

        staged = stack.enter_context(takeover.staged_replica(session, check))
        if staged is None or not staged.metadata:
            return pending(takeover.SYNC_PENDING, "the vault replica is missing, incomplete or invalid")
        marker = staged.canonical
        current_fence = session.fence_client.collection_store_fence()
        if current_fence.enrolled and current_fence.store_id != marker["store_id"]:
            return pending(takeover.DIVERGED, "the replica differs from the coordinator store")
        if raw is not None:
            hint = authority.parse_marker(session.root, raw)
            if hint["store_id"] != marker["store_id"]:
                return pending(takeover.DIVERGED, "the replica differs from the backup identity")
            try:
                authority.require_marker(staged.reader, hint, root=session.root)
            except CollectionStoreError as error:
                if error.code != "COLLECTION_STORE_MARKER_CONFLICT":
                    raise
                return pending(takeover.SYNC_PENDING, error.reason)
            marker = {**marker, "authority_epoch": hint["authority_epoch"],
                      "collection_store_fence": hint["collection_store_fence"]}
        elif not current_fence.enrolled:
            return pending(takeover.SYNC_PENDING, "restore lacks a matching store fence")
        refusal = takeover.replica_adoptability(staged.metadata, raw)
        if refusal is not None:
            return pending(*refusal)
    fence = _store_fence(session.fence_client, session.fence_client.collection_store_fence(), marker,
                         initial=marker["collection_store_fence"]["generation"] == 1)
    return raw, marker, fence, staged


def _takeover_facts(session, runtime, token, raw, marker, fence):
    """What the takeover decision reads; none of it needs the mutation boundary, or the store's migration."""
    recorded = _acquired(runtime, fence, "COLLECTION_STORE_LEASE_REQUIRED").collection_store_head
    signature = takeover.replica_signature(session.root)
    local, relation, diverged = None, None, False
    if session.path.exists():
        local = runtime.sample_head()
        with closing(connection.open_head_reader(session.path)) as reader:
            diverged = reader.execute("SELECT 1 FROM store_meta WHERE key=?",
                                      (schema.META_REPLICA_DIVERGENCE,)).fetchone() is not None
            if recorded is not None:
                relation = chain.head_relation(reader, recorded.commit_seq, recorded.head_hash)
    facts = dict(marker=marker, raw_marker=raw, recorded=recorded, local=local,
                 local_relation=relation, token=token)
    return facts, diverged, signature


def _settle(session, runtime, token, facts, staged, action, reason, check):
    """Under the boundary: revalidate cheaply what the decision read, then apply it."""
    manager = session.manager
    state_migration._require_collection_store_recovery(session, token)
    # An adoption replaces the live store with the staged replica, which staging migrated.
    if action != "adopt" and session.path.exists():
        failure = _migrate_schema(session, token)
        if failure is not None:
            return takeover.SCHEMA_PENDING, failure
    if action == takeover.DIVERGED:
        # Durable once the handle is idle; until then the in-memory verdict refuses writes.
        if runtime.retire_idle_handle():
            with session.writer(token) as writer, writer.handle.transaction() as conn:
                conn.execute("INSERT INTO store_meta(key,value) VALUES (?,?)", (
                    schema.META_REPLICA_DIVERGENCE, json.dumps({"reason": reason}, sort_keys=True)))
        return action, reason
    if (authority.read_marker(session.root) != facts["raw_marker"]
            or (runtime.sample_head() if session.path.exists() else None) != facts["local"]
            or (staged is not None and (staged.signature is None
                                        or takeover.replica_signature(session.root) != staged.signature))):
        return takeover.BUSY, "the store or vault replica changed during takeover"
    if not runtime.retire_idle_handle():
        return takeover.BUSY, "the live store is still borrowed"
    try:
        if action == "adopt":
            # The installed copy has the current schema, so the manifest records its IDs first.
            state_migration.record_collection_store_schema(session.root, authority_check=lambda: session.require(token))
            runtime._identity = takeover.install(session, staged, token, check)
        elif action == "continue":
            with session.writer(token) as writer:
                runtime._identity = takeover.continue_in_place(writer, staged, token)
    except CollectionStoreError as error:
        if error.code != takeover.BUSY:
            raise
        return takeover.BUSY, str(error)
    # Every admission enrolls compatibility, so a crash after installation recovers here.
    state_migration.record_collection_store_compatibility(
        session.root, authority_check=lambda: session.require(token))
    with session.writer(token) as writer:
        try:
            _upgrade_marker(session, writer, token)
        except OSError:
            # Atomic publication can finish before durability reports an error; the same canonical marker retries next use.
            return takeover.BUSY, "marker ownership publication is pending"
        published = _publish_current_epoch(session, writer, token)
    if published.status == "published":
        # Record this instance's head now, so the next holder adopts this replica.
        manager._renew_collection_store(token)
        return "admitted", None
    code = {"diverged": takeover.DIVERGED, "custody_unverified": takeover.CUSTODY_LOST}.get(
        published.status, "COLLECTION_STORE_FLUSH_PENDING")
    return code, f"replica publication: {published.reason or published.status}"


def _resume_locked(session, fence_client, *, preparation_token=None):
    """Finish the one pending intent, a create or a migration, in the A5 order after its store is live."""
    manager = session.manager
    intent = None
    # An intent an earlier release left stays at its schema until a writer below migrates it,
    # so the intent and the head are read through the head reader.
    if session.path.exists():
        with closing(connection.open_head_reader(session.path)) as reader:
            intent = authority.pending_create(reader)
    if intent is not None and intent["kind"] == authority.EXPORT:
        raise export_pending()
    if session.production:
        if intent is not None and intent["kind"] == authority.MIGRATE:
            capability.require_collections_migrate()
        else:
            capability.require_records_summary(session.root)
    runtime = session.runtime()
    with closing(connection.open_head_reader(session.path)) as reader:
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
    supersedes = intent.get("supersedes")
    # A supersede moves the fence from the unadmitted store it binds, so like a first cut it revokes the token.
    initial_cut = not fence.enrolled or supersedes is not None and fence.store_id == supersedes["store_id"]
    if initial_cut:
        if preparation_token is None:
            preparation_token = manager.ensure_writer().fencing_token
        # This writer may migrate the store, which follows the recovery check as every writer here does.
        state_migration._require_collection_store_recovery(session, preparation_token)
        # A first cut binds every writer to this store, so a marker that already names another
        # store must refuse before the cut, whoever prepared the store.
        with session.writer(preparation_token) as writer:
            recovered = _recover_create(writer)
            if recovered["status"] == "conflict":
                return recovered
        session.require(preparation_token)
    fence = _store_fence(fence_client, fence, target, initial=intent["expected_marker"] is None,
                         supersedes=supersedes)
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
        try:
            _require_create_file_ownership(writer, intent)
        except CollectionStoreError as error:
            return {**recovered, "status": "pending", "reason": error.code}
        published = _publish_current_epoch(session, writer, token)
        if published.status != "published":
            return {**recovered, "status": "pending", "reason": published.reason or published.status}
        current = (published.commit_seq, published.head_hash)
        try:
            manager._renew_collection_store(token)
            try:
                _install_marker(session, token, fence_client, target, intent, current, writer)
            except CollectionStoreError as error:
                if error.code != "COLLECTION_STORE_MARKER_CONFLICT":
                    raise
                with manager._report_lock:
                    runtime._admitted_token = None
                return {**recovered, "status": "pending", "reason": error.code}
            if intent["kind"] == authority.CREATE:
                # A migration publishes no view: its imported files are already current.
                writer.reconcile_views()
            result = _recover_create(writer)
            if result["status"] != "marker_admitted":
                raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "marker changed after cutover")
            with writer.handle.transaction() as conn:
                session.require(token)
                if authority.marker_status(writer, intent) != "marker_admitted":
                    raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "marker changed before cleanup")
                conn.execute("DELETE FROM store_meta WHERE key=?", (authority.PENDING_CREATE,))
            if _upgrade_marker(session, writer, token):
                published = _publish_current_epoch(session, writer, token)
                if published.status != "published":
                    return {**result, "status": "pending", "reason": published.reason or published.status}
            return result
        except BaseException:
            with manager._report_lock:
                runtime._admitted_token = None
            raise


def _publication_authority(session, token):
    def check():
        session.manager._renew_collection_store(token, due_only=True)
        return session.require(token)

    return check


def _publish_current_epoch(session, writer, token):
    from . import legacy_originals

    manager = session.manager
    deadline = time.monotonic() + 30
    check = _publication_authority(session, token)
    if not _publication_custody(session, token):
        return replica.PublicationResult("custody_unverified", reason=session.custody_reason)
    legacy_originals.backfill(writer, lambda: session.require(token))
    pending = replica.recover_replica(session.root, writer.handle, authority_check=check, deadline=deadline)
    if pending.status != "published" and (pending.reason is not None or writer.connection.execute(
        "SELECT 1 FROM store_meta WHERE key=?", (schema.META_PENDING_REPLICA_PUBLICATION,),
    ).fetchone() is not None):
        return pending
    with writer.handle.transaction() as conn:
        conn.execute("INSERT OR REPLACE INTO store_meta(key,value) VALUES (?,?)", (schema.META_LEASE_EPOCH, str(token)))
        conn.execute("INSERT OR REPLACE INTO store_meta(key,value) VALUES (?,?)", (authority.MARKER_REQUIRED, "1"))
    # A same-head archive backfill must survive loss of the live store before views replace its sources.
    force = legacy_originals.needs_replica_copy(session.root, writer.connection)
    published = replica.publish_replica(session.root, writer.handle, authority_check=check, deadline=deadline,
                                       **({"force": True} if force else {}))
    if published.status == "published":
        if chain.verify_store_chain(writer.connection) != (published.commit_seq, published.head_hash):
            raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "publication did not reach current head")
        with manager._report_lock:
            session.require(token)
            manager._collection_store.record_admission(token)
    return published


def _require_fence(fence, target, code="COLLECTION_STORE_MARKER_CONFLICT"):
    if fence.store_id != target["store_id"] or fence.generation != target["collection_store_fence"]["generation"]:
        raise CollectionStoreError(code, "store fence differs from the marker")
    return fence


def _store_fence(fence_client, fence, target, *, initial, supersedes=None):
    """Cut an unenrolled coordinator over to ``target``'s store once, then bind its fence.

    ``initial`` says the caller's marker basis may make that first cut: a create that
    replaces no marker, or a copied vault whose marker carries the first generation.
    The cut lands on the generation after the fence's newest retired one (0 if never
    enrolled), so a marker minted before a retire never binds the next enrolment.
    ``supersedes`` is the unadmitted store and generation a first enrolment's fence binds
    (P1b.3c): the fence moves from it to ``target`` by one CAS at the next generation.
    """
    if initial and fence.enrolled and supersedes is not None and fence.store_id == supersedes["store_id"]:
        if (fence.generation != supersedes["generation"]
                or target["collection_store_fence"]["generation"] != fence.generation + 1):
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "superseded fence basis differs")
        fence = fence_client.transition_collection_store_fence(
            expected_generation=fence.generation, store_id=target["store_id"])
    elif not fence.enrolled:
        if not initial or target["collection_store_fence"]["generation"] != fence.generation + 1:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "initial fence basis differs")
        fence = fence_client.transition_collection_store_fence(
            expected_generation=fence.generation, store_id=target["store_id"])
    return _require_fence(fence, target)


def _acquired(runtime, fence, code):
    """The current token's acquisition, bound to the coordinator's store fence."""
    acquired = runtime._acquisition
    if (acquired is None or acquired.fencing_token != runtime.manager._fencing_token
            or acquired.collection_store_fence_generation != fence.generation
            or acquired.required_collection_store_capability != fence.capability):
        raise CollectionStoreError(code, "acquisition fence differs")
    return acquired


def holds_head(conn, recorded, store_id, instance_id, *, lineage=None):
    """Require this store's instance and chain; a validated lineage also accepts its recorded ancestors."""
    found = conn.execute(
        "SELECT store_head_hash FROM txns WHERE commit_seq=?", (recorded.commit_seq,),
    ).fetchone() if recorded.commit_seq else (None,)
    belongs = recorded.instance_id == instance_id or (lineage is not None and any(
        entry["instance_id"] == recorded.instance_id for entry in lineage))
    return recorded.store_id == store_id and belongs and found == (recorded.head_hash,)


def _verify_acquisition(writer, runtime, fence, target):
    _require_fence(fence, target, "COLLECTION_STORE_CREATE_CONFLICT")
    recorded = _acquired(runtime, fence, "COLLECTION_STORE_CREATE_CONFLICT").collection_store_head
    if recorded is not None and not holds_head(
            writer.connection, recorded, target["store_id"], runtime.sample_head().instance_id):
        raise CollectionStoreError("COLLECTION_STORE_SYNC_PENDING", "acquired head is not in the local chain")


def _require_create_file_ownership(writer, intent):
    root = writer.root
    raw = authority.read_marker(root)
    expected = None if intent["expected_marker"] is None else intent["expected_marker"].encode()
    target_raw = intent["target_marker"].encode()
    if raw not in (expected, target_raw):
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "create marker preimage changed")
    current = None if raw is None else authority.parse_marker(root, raw)
    target = authority.parse_marker(root, target_raw)
    if target["version"] == 1:
        target = authority.upgraded_marker(root, writer.connection, target)
    if intent["kind"] == authority.MIGRATE:
        # The migrated collections leave file authority together, so each is checked only
        # against the file collections the target marker leaves in files.
        migrated = authority.intent_collection_ids(intent)
        for entry in target["collections"]:
            if entry["collection_id"] in migrated:
                authority.require_new_file_ownership(root, target, entry)
        return
    # An interrupted create may wait behind a new file declaration; retaining its intent costs only C publication.
    authority.require_new_file_ownership(root, current, target["collections"][-1])


def _install_marker(session, token, fence_client, target, intent, current, writer):
    session.verify_custody()
    session.require(token)
    fence = fence_client.collection_store_fence()
    if (fence.store_id != target["store_id"]
            or fence.generation != target["collection_store_fence"]["generation"]
            or chain.verify_store_chain(writer.connection) != current):
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "cutover authority changed")
    _require_create_file_ownership(writer, intent)
    expected = None if intent["expected_marker"] is None else intent["expected_marker"].encode()
    _replace_marker(session, token, expected, intent["target_marker"].encode())


def _upgrade_marker(session, writer, token):
    from . import ownership

    proof = ownership.current(session.root, conn=writer.connection, private=not session.production)
    if proof.marker is not None:
        # Admission validates all canonical projection containment, including collections absent from the hint.
        authority.require_projection_ownership(writer.connection, proof.marker, root=session.root)
    raw = authority.read_marker(session.root)
    if raw is None:
        return
    marker = authority.parse_marker(session.root, raw)
    authority.require_marker(writer.connection, marker, root=session.root)
    if marker["version"] == 2:
        return
    # The same validated canonical head and fence determine every retry; no editable projection supplies ownership.
    chain.verify_store_chain(writer.connection)
    upgraded = authority.upgraded_marker(session.root, writer.connection, marker)
    state_migration.record_collection_store_compatibility(session.root, authority_check=lambda: session.require(token))
    with session.manager._report_lock:
        session.manager._collection_store._admitted_token = None
    _replace_marker(session, token, raw, json.dumps(upgraded, sort_keys=True, separators=(",", ":")).encode())
    return True


def _replace_marker(session, token, expected, raw):
    """Publish one marker atomically under the existing custody, lease and mutation boundary."""
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
            if (intent["kind"] != authority.CREATE or intent["request_id"] != request_id
                    or intent["request_hash"] != request_hash
                    or intent["expected_marker"] != (None if self.expected is None else self.expected.decode())
                    or intent["target_marker"] != self.target.decode() or replay is None):
                raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "another create intent or marker basis is pending")
            self.txn_id = intent["txn_id"]
        elif replay is not None:
            raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "create receipt lacks its admission intent")

    def recheck(self, root):
        if authority.read_marker(root) != self.expected:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "create marker basis changed")
        old = None if self.expected is None else authority.parse_marker(root, self.expected)
        # Rechecking at commit prevents C from seizing a late file source; a false conflict only delays this create.
        authority.require_new_file_ownership(root, old, self.entry)

    def record(self, manifest, txn):
        from . import ownership

        writer = self.writer
        writer._publication.deferred_create = manifest.collection_id
        self.txn_id = txn["txn_id"]
        target = authority.parse_marker(writer.root, self.target)
        sid = writer._publication.identity["store_id"]
        old = None if self.expected is None else authority.parse_marker(writer.root, self.expected)
        canonical, _ = ownership.declarations(writer.root, writer.connection)
        self.entry = authority.marker_entry(manifest.collection_id, manifest.path, sid, manifest.semantic_profile,
                                            manifest.storage.source, manifest.storage.strategy)
        wire_entry = authority.marker_entry(manifest.collection_id, manifest.path, sid, manifest.semantic_profile,
                                            manifest.storage.source if target["version"] == 2 else None,
                                            manifest.storage.strategy)
        existing = set() if canonical is None else authority.intent_collection_ids(canonical)
        if (target["store_id"] != sid or
                authority.intent_collection_ids(target) != existing | {manifest.collection_id}
                or authority.selected_entry(writer.root, target, manifest.collection_id) != wire_entry
                or target["authority_epoch"] != (1 if old is None else old["authority_epoch"] + 1)
                or (old is not None and (old["store_id"] != sid or
                    old["collection_store_fence"] != target["collection_store_fence"]))
                or authority.read_marker(writer.root) != self.expected):
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "create marker basis differs")
        for target_entry in target["collections"]:
            # The writer records admission immediately before inserting the new collection.
            if target_entry["collection_id"] != manifest.collection_id:
                authority.require_selected(writer.connection, target, target_entry, root=writer.root)
        writer._recheck_guard(self)
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
    """The pending intent's marker status, after proving its transactions are in the current chain."""
    writer.handle.require_write_authority()
    with writer._authorization(), writer.read_snapshot():
        intent = authority.pending_create(writer.connection)
        if intent is None:
            return None
        for entry in intent["collections"]:
            writer._operation.require_collection(entry["collection_id"], complete=True)
        head = chain.verify_store_chain(writer.connection)
        receipt = authority.require_intent_binding(writer.connection, intent)
        if intent["kind"] == authority.MIGRATE:
            return {"status": authority.marker_status(writer, intent), "kind": authority.MIGRATE,
                    "collection_ids": sorted(authority.intent_collection_ids(intent)),
                    "head": {"commit_seq": head[0], "head_hash": head[1]}}
        return {"status": authority.marker_status(writer, intent), "collection_id": intent["collection_id"],
                "txn_id": intent["txn_id"], "receipt": receipt,
                "head": {"commit_seq": head[0], "head_hash": head[1]}}
