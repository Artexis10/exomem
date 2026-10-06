"""Private create/restart/takeover producer; public routes and launch support remain closed.

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
from contextlib import ExitStack, closing, contextmanager, suppress
from pathlib import Path

from .. import held_fs, state_migration, state_paths, vault, writer_lease
from .. import structured_collections as collections
from ..cli_ops import OpError
from . import authority, chain, connection, custody, owner, replica, schema, takeover
from .connection import CollectionStoreError


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
        if not self.check() or not manager.config.enabled:
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
    session.verify_custody()
    _ProducerSession.bind(session, manager)
    _bind_fence_client(session, fence_client)
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
    raw, marker, fence = _routed_store(session)
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

        with ExitStack() as stack:
            staged = None
            if action == "stage":
                heads = [None if recorded is None else (recorded.commit_seq, recorded.head_hash),
                         None if local is None else (local.commit_seq, local.head_hash)]
                staged = stack.enter_context(takeover.staged_replica(session, check, heads))
                action, reason = ((takeover.SYNC_PENDING, "the vault replica has not arrived")
                                  if staged is None else takeover.decide(staged=staged, **facts))
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
    return runtime, _routed_store(session)[2]


def _stale_preview():
    return CollectionStoreError("COLLECTION_STORE_ADOPT_PREVIEW_STALE",
                                "the store, replica or recorded head changed since the preview; preview again")


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
            if not runtime.retire_idle_handle():
                raise CollectionStoreError(takeover.BUSY, "the live store is still borrowed")
            abandoned = sealed["preview"]["abandoned_publication"]
            with session.writer(token) as writer:
                with writer.handle.transaction(resolve_divergence=True) as conn:
                    (predecessor,) = conn.execute("SELECT value FROM store_meta WHERE key=?", (
                        schema.META_LAST_PUBLISHED_REPLICA_SHA256,)).fetchone() or (None,)
                    identity = owner.record_fork(conn, sealed["preview"], why=why, token=token)
                with manager._report_lock:
                    runtime._identity = identity
                    # A pending takeover verdict described the old tenure.
                    runtime._takeover = None
                kept = None if abandoned is None else replica.discard_abandoned_publication(
                    session.root, writer.handle, abandoned, predecessor=predecessor,
                    authority_check=_publication_authority(session, token), deadline=time.monotonic() + 30)
                published = _publish_current_epoch(session, writer, token)
        workspace = {} if abandoned is None else {"abandoned_workspace": kept or "removed"}
        if published.status != "published":
            return {"status": "pending", "reason": published.reason or published.status, **workspace, **sealed}
        manager._renew_collection_store(token)
        return {"status": "adopted", "instance_id": identity[1], **workspace, **sealed}


def reconcile_store(session, manager, *, why, fence_client, preview_id=None):
    """Owner reconcile (§15 item 5), preview-first: foreign evidence becomes held corrections.

    Every item a preserved foreign store changed after its common ancestor with this
    one is held with its values and diagnostics; no canonical row changes. A diverged
    store refuses: adopt-local decides which side continues first.
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
            with session.writer(token) as writer:
                result = writer.hold_store_delta(items, reconciled=sealed["preview"]["reconciled"], why=why)
    return {"status": "held", **result, **sealed}


def _coordinator_head():
    """The coordinator's recorded store head, or ``owner.UNKNOWN`` without a configured coordinator."""
    config = writer_lease.LeaseConfig.from_env()
    if not config.enabled:
        return owner.UNKNOWN
    return writer_lease.LeaseCoordinatorClient(config).status().collection_store_head


def _route_step(root):
    """Adopt-local while this store does not continue the vault's replica, then its reconcile step."""
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


def _require_no_service(config):
    """Refuse an out-of-service apply while any holder's lease is live (A11, as `collections migrate`).

    Taking the lease beside a running service would hand its token back under it and
    change the store identity it serves.
    """
    record = writer_lease.LeaseCoordinatorClient(config).status()
    if record.holder is not None and record.expires_at is not None and record.expires_at > time.time():
        raise CollectionStoreError(
            "COLLECTION_STORE_SERVICE_ACTIVE",
            f"a service ({record.holder}) holds this vault's writer lease; apply through its "
            "maintain_memory(mode=\"collections-store-adopt-local\") or stop it first")


def _apply_step(session, manager, fence_client, step, *, why, preview_id):
    if step == "adopt-local":
        return adopt_local(session, manager, why=why, fence_client=fence_client, preview_id=preview_id)
    open_store(session, manager, fence_client=fence_client)
    return reconcile_store(session, manager, why=why, fence_client=fence_client, preview_id=preview_id)


def adopt_local_route(vault_root, *, why=None, preview_id=None):
    """The one owner route behind `exomem collections adopt-local` and its maintain_memory mode (A3).

    Preview-first: the preview reads without the writer lease and names its step,
    adopt-local or, once this store continues, reconciling preserved foreign evidence
    into held corrections. Applying runs that step's `adopt_local` or `reconcile_store`
    with ``preview_id``. In a process that serves the vault (the service answering
    maintain_memory) it runs on that service's own session and lease, which it keeps.
    Elsewhere (the CLI) it refuses while any service holds the lease; otherwise it runs
    in its own producer session under the configured lease and hands the lease back
    with the flushed head.
    """
    from ..governance.principal import OWNER_AUDIENCE, effective_principal

    who = effective_principal()
    if not (who.resolved and who.audience_id == OWNER_AUDIENCE):
        raise CollectionStoreError("COLLECTION_STORE_OWNER_REQUIRED", "adopt-local is owner-only")
    root = Path(vault_root).resolve()
    step, sealed = _route_step(root)
    if preview_id is None:
        if sealed["preview"].get("recorded_head") == owner.UNKNOWN:
            return {"step": step, **sealed, "warnings": [
                "no writer lease is configured here, so the coordinator's head was not read; "
                "an apply needs a preview taken with the configured writer lease"]}
        return {"step": step, **sealed}
    serving = _serving_session(root)
    if serving is not None:
        fence_client = serving.fence_client or writer_lease.configured_schema_fence_operator_client()
        if fence_client is None:
            raise CollectionStoreError("COLLECTION_STORE_LEASE_REQUIRED", "adopt-local needs the configured writer lease")
        return {"step": step, **_apply_step(serving, serving.manager, fence_client, step, why=why,
                                            preview_id=preview_id)}
    fence_client = writer_lease.configured_schema_fence_operator_client()
    if fence_client is None:
        raise CollectionStoreError("COLLECTION_STORE_LEASE_REQUIRED", "adopt-local needs the configured writer lease")
    config = writer_lease.LeaseConfig.from_env()
    _require_no_service(config)
    with production_session(root) as session:
        manager = writer_lease.LeaseManager(config)
        try:
            result = _apply_step(session, manager, fence_client, step, why=why, preview_id=preview_id)
        except BaseException:
            # The refusal is the answer; an unadmitted token keeps its lease until it expires.
            with suppress(Exception):
                manager.close()
            raise
        manager.close()
    return {"step": step, **result}


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


def _routed_store(session):
    """The marker this takeover serves, with the coordinator's store fence bound to it.

    A copied vault meeting a fresh coordinator cuts the fence over from its marker,
    which revokes any earlier token, so this precedes the lease acquisition.
    """
    if session.path.exists():
        with closing(connection.open_reader(session.path)) as reader:
            if authority.pending_create(reader) is not None:
                raise CollectionStoreError("COLLECTION_STORE_LEASE_REQUIRED",
                                           "create recovery owns this store; resume that create")
    raw = authority.read_marker(session.root)
    if raw is None:
        raise CollectionStoreError("COLLECTION_STORE_UNAVAILABLE", "no collection here routes to a store")
    marker = authority.parse_marker(session.root, raw)
    fence = _store_fence(session.fence_client, session.fence_client.collection_store_fence(), marker,
                         initial=marker["collection_store_fence"]["generation"] == 1)
    return raw, marker, fence


def _takeover_facts(session, runtime, token, raw, marker, fence):
    """What the takeover decision reads; none of it needs the mutation boundary."""
    recorded = _acquired(runtime, fence, "COLLECTION_STORE_LEASE_REQUIRED").collection_store_head
    signature = takeover.replica_signature(session.root)
    local, relation, diverged = None, None, False
    if session.path.exists():
        local = runtime.sample_head()
        with closing(connection.open_reader(session.path)) as reader:
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
        published = _publish_current_epoch(session, writer, token)
    if published.status == "published":
        # Record this instance's head now, so the next holder adopts this replica.
        manager._renew_collection_store(token)
        return "admitted", None
    code = {"diverged": takeover.DIVERGED, "custody_unverified": takeover.CUSTODY_LOST}.get(
        published.status, "COLLECTION_STORE_FLUSH_PENDING")
    return code, f"replica publication: {published.reason or published.status}"


def _resume_locked(session, fence_client, *, preparation_token=None):
    manager = session.manager
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
    if initial_cut:
        if preparation_token is None:
            preparation_token = manager.ensure_writer().fencing_token
            with session.writer(preparation_token) as writer:
                recovered = _recover_create(writer)
                if recovered["status"] == "conflict":
                    return recovered
        session.require(preparation_token)
    fence = _store_fence(fence_client, fence, target, initial=intent["expected_marker"] is None)
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


def _publication_authority(session, token):
    def check():
        session.manager._renew_collection_store(token, due_only=True)
        return session.require(token)

    return check


def _publish_current_epoch(session, writer, token):
    manager = session.manager
    deadline = time.monotonic() + 30
    check = _publication_authority(session, token)
    if not _publication_custody(session, token):
        return replica.PublicationResult("custody_unverified", reason=session.custody_reason)
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
            manager._collection_store.record_admission(token)
    return published


def _require_fence(fence, target, code="COLLECTION_STORE_MARKER_CONFLICT"):
    if fence.store_id != target["store_id"] or fence.generation != target["collection_store_fence"]["generation"]:
        raise CollectionStoreError(code, "store fence differs from the marker")
    return fence


def _store_fence(fence_client, fence, target, *, initial):
    """Cut an unenrolled coordinator over to ``target``'s store once, then bind its fence.

    ``initial`` says the caller's marker basis may make that first cut: a create that
    replaces no marker, or a copied vault whose marker carries the first generation.
    """
    if not fence.enrolled:
        if not initial or fence.generation != 0:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "initial fence basis differs")
        fence = fence_client.transition_collection_store_fence(expected_generation=0, store_id=target["store_id"])
    return _require_fence(fence, target)


def _acquired(runtime, fence, code):
    """The current token's acquisition, bound to the coordinator's store fence."""
    acquired = runtime._acquisition
    if (acquired is None or acquired.fencing_token != runtime.manager._fencing_token
            or acquired.collection_store_fence_generation != fence.generation
            or acquired.required_collection_store_capability != fence.capability):
        raise CollectionStoreError(code, "acquisition fence differs")
    return acquired


def _verify_acquisition(writer, runtime, fence, target):
    _require_fence(fence, target, "COLLECTION_STORE_CREATE_CONFLICT")
    recorded = _acquired(runtime, fence, "COLLECTION_STORE_CREATE_CONFLICT").collection_store_head
    if recorded is not None:
        found = writer.connection.execute(
            "SELECT store_head_hash FROM txns WHERE commit_seq=?", (recorded.commit_seq,),
        ).fetchone() if recorded.commit_seq else (None,)
        if (recorded.store_id != target["store_id"] or found != (recorded.head_hash,)
                or recorded.instance_id != runtime.sample_head().instance_id):
            raise CollectionStoreError("COLLECTION_STORE_SYNC_PENDING", "acquired head is not in the local chain")


def _install_marker(session, token, fence_client, target, intent, current, writer):
    session.verify_custody()
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
