"""Vault-bound lifetime for admitted collection stores.

Admission comes only from a trusted producer session (``admission``) that holds
custody, fence and takeover authority. Release
capabilities control serving; compatibility gates supported launchers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, closing, contextmanager, suppress
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

from .. import call_spans, foreground_priority, state_paths
from . import connection, legacy_originals, replica, schema

logger = logging.getLogger(__name__)

#: A9/N4: steady writes publish the replica at most once per interval, outside the write path.
PUBLISH_INTERVAL_SECONDS = 60.0
#: A burst of commits shares one publication once writes pause this long.
PUBLISH_SETTLE_SECONDS = 1.0
#: The cutoff share for starting optional view work; an in-flight filesystem call or
#: required finalization may finish later. Pending views leave time for replica publication.
DRAIN_SHARE = 0.5
#: The `_Collections/` watch (A4): stat the replica every tick; hash it only when that changes.
WATCH_SECONDS = 1.0
_BUSY_RETRY_SECONDS = 0.05
# Producer-bound runtimes by vault: the export flush and the in-service owner route find them.
_SERVING = weakref.WeakValueDictionary()


class _Publisher:
    """Coalesced replica publication and the `_Collections/` watch for one admitted store.

    Publication copies a pinned snapshot outside the vault-wide mutation boundary, so
    writes, file-collection writes included, proceed during the copy and verification.
    Brief steps hold the boundary at idle instants and compare held-file witnesses
    before each effect; a write arriving during one waits for it. The watch
    hashes the replica against the digest this instance last published when its stat
    signature changes. A foreign rewrite that keeps inode, size and mtime is found by
    the next publication's check-then-swap, which records the divergence and preserves
    the foreign bytes; the watch decides nothing.
    """

    def __init__(self, runtime):
        self.runtime = runtime
        self.condition = threading.Condition()
        self.dirty = None  # (first, latest) unpublished commit, monotonic
        self.published = None  # last coalesced publication, monotonic
        self.foreign = False
        self.retry = None
        self.signature = None
        threading.Thread(target=self.run, name="exomem-collection-replica", daemon=True).start()

    def note_commit(self):
        with self.condition:
            now = time.monotonic()
            self.dirty = (now if self.dirty is None else self.dirty[0], now)
            self.condition.notify()

    def due(self, now):
        due = None
        if self.dirty is not None:
            first, latest = self.dirty
            due = min(latest + PUBLISH_SETTLE_SECONDS, first + PUBLISH_INTERVAL_SECONDS)
            if self.published is not None:
                due = max(due, self.published + PUBLISH_INTERVAL_SECONDS)
        if self.foreign:
            due = now
        if due is not None and self.retry is not None:
            due = max(due, self.retry)
        return due

    def run(self):
        manager = self.runtime.manager
        while True:
            with self.condition:
                now = time.monotonic()
                due = self.due(now)
                wait = WATCH_SECONDS if due is None else min(WATCH_SECONDS, due - now)
                if wait > 0:
                    self.condition.wait(wait)
            if manager._stop.is_set() or manager._store_closed:
                return
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a failed publication retries after one interval
                logger.warning("collection store replica publication remains pending", exc_info=True)
                with self.condition:
                    self.published, self.foreign = time.monotonic(), False

    def tick(self):
        runtime, manager = self.runtime, self.runtime.manager
        token = manager._fencing_token
        # Release already flushed and an unadmitted takeover rechecks the replica itself:
        # neither has anything to publish.
        if token is None or not runtime.reporting_ready(token):
            with self.condition:
                self.dirty, self.foreign = None, False
            return
        self.watch()
        now = time.monotonic()
        with self.condition:
            due = self.due(now)
            if due is None or due > now:
                return
            dirty, foreign = self.dirty, self.foreign
        # A diverged store waits for the owner; the store is read only when something is due.
        if runtime._meta(schema.META_REPLICA_DIVERGENCE) != [None]:
            with self.condition:
                self.dirty, self.foreign = None, False
            return
        if not foreign and runtime.replica_current():
            with self.condition:
                if self.dirty == dirty:
                    self.dirty = None
            return
        head = manager._publish_collection_store(token, deadline=now + 30)
        with self.condition:
            if head is None:
                self.retry = time.monotonic() + _BUSY_RETRY_SECONDS
                return
            self.published, self.foreign, self.retry = now, False, None
            self.dirty = None if self.dirty == dirty else (now, self.dirty[1])

    def watch(self):
        from .takeover import replica_signature

        signature = replica_signature(self.runtime.root)
        if signature == self.signature:
            return
        self.signature = signature
        if not self.runtime.replica_matches():
            with self.condition:
                self.foreign = True


def reader_pool(root, path):
    """The reader pool of the serving runtime's cached writer for the store at ``path``, else None.

    None means open a fresh reader, as every read did before the pool: no runtime serves ``root``
    here, it holds no live writer, or that writer is not on this store file (R2).
    """
    runtime = _SERVING.get(root) or _SERVING.get(Path(root).resolve())
    return None if runtime is None else runtime.reader_pool(path)


def flush_for_export(vault_root, *, timeout=30.0):
    """Synchronously publish the replica of an admitted store this process serves.

    Returns the flushed head, or None when no flush runs here and the replica already
    carries the live head. When this process cannot flush (no admitted store, a lost
    lease or lost custody) and the live store is ahead of the head it last published,
    it refuses FLUSH_PENDING rather than let an export drop acknowledged rows.
    """
    root = Path(vault_root).resolve()
    runtime = _SERVING.get(root)
    token = None if runtime is None else runtime.manager._fencing_token
    if token is not None and runtime.reporting_ready(token):
        head = runtime.manager._release_collection_store(
            token, deadline=time.monotonic() + timeout, release=False)
        if not head:
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_FLUSH_PENDING", "the replica could not be flushed before export")
        return head
    path = connection.store_path(root)
    if path.exists() and not _replica_carries_live_head(path):
        raise connection.CollectionStoreError(
            "COLLECTION_STORE_FLUSH_PENDING",
            "the live store is ahead of its published replica and this process cannot flush it")
    return None


def _replica_carries_live_head(path):
    """Whether the replica this store last published carries its committed head."""
    with closing(connection.open_reader(path)) as reader:
        reader.execute("BEGIN")
        meta = legacy_originals.metadata(reader)
    sequence, head = int(meta[schema.META_COMMIT_SEQ]), meta.get(schema.META_STORE_HEAD_HASH)
    published = json.loads(meta.get(schema.META_PUBLISHED_REPLICA_HEAD) or "null")
    if published is not None:
        return (published["commit_seq"], published["head_hash"]) == (sequence, head)
    # A tenure adopted at this head has published nothing new since the replica it adopted.
    entry = json.loads(meta[schema.META_LINEAGE])[-1]
    return sequence == 0 or (meta.get(schema.META_LAST_PUBLISHED_REPLICA_SHA256) is not None
                             and entry["adopted_from"] is not None and entry["adopted_at_commit_seq"] == sequence)


class _Scope:
    def __init__(self):
        self.active = True
        self.thread = threading.get_ident()

    def check(self):
        if not self.active:
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_CHECKOUT_CLOSED", "the preview checkout has ended"
            )
        if self.thread != threading.get_ident():
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_WRITER_THREAD", "the opening thread owns the writer connection"
            )

    def wrap(self, value):
        from .census import Census
        from .governance import OperationAuthorization, ReleaseCache
        from .inspection import InspectionCache
        from .writer import CollectionWriter

        # Python 3.11 paths implement a deprecated no-op context manager;
        # they are ordinary values, not resources borrowed from this checkout.
        if isinstance(value, Path):
            return value
        if isinstance(value, (
            sqlite3.Connection, sqlite3.Cursor, connection.WriterConnection,
            CollectionWriter, OperationAuthorization, ReleaseCache, InspectionCache,
            Census,
            AbstractContextManager, Iterator,
        )):
            return _Borrowed(value, self)
        return value


class _Borrowed:
    """Guard saved preview bindings, SQL cursors and deferred context managers."""

    def __init__(self, value, scope):
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "_scope", scope)

    def __getattr__(self, name):
        self._scope.check()
        value = getattr(self._value, name)
        wrapped = self._scope.wrap(value)
        if wrapped is not value or not callable(value):
            return wrapped

        @wraps(value)
        def call(*args, **kwargs):
            self._scope.check()
            # Internal evidence uses handle identity, not proxy identity.
            for arg in (*args, *kwargs.values()):
                if isinstance(arg, _Borrowed):
                    arg._scope.check()
            args = tuple(arg._value if isinstance(arg, _Borrowed) else arg for arg in args)
            kwargs = {key: arg._value if isinstance(arg, _Borrowed) else arg
                      for key, arg in kwargs.items()}
            return self._scope.wrap(value(*args, **kwargs))
        return call

    def __setattr__(self, name, value):
        self._scope.check()
        setattr(self._value, name, value)

    def __iter__(self):
        self._scope.check()
        return self

    def __next__(self):
        self._scope.check()
        return self._scope.wrap(next(self._value))

    def __enter__(self):
        self._scope.check()
        return self._scope.wrap(self._value.__enter__())

    def __exit__(self, *args):
        self._scope.check()
        return self._value.__exit__(*args)


@contextmanager
def borrowed_writer(root, handle, manager):
    """Legacy callers retain ownership, but every bound use counts as a borrower."""
    from ..writer_lease import _ACTIVE_LEASE_MANAGER
    from .writer import CollectionWriter

    with manager._collection_store_checkout():
        handle.require_owner_thread()
        scope = _Scope()
        manager_context = _ACTIVE_LEASE_MANAGER.set(manager)
        try:
            yield scope.wrap(CollectionWriter(root, handle))
        finally:
            scope.active = False
            _ACTIVE_LEASE_MANAGER.reset(manager_context)


class CollectionStoreRuntime:
    """One cached operation writer and a fresh writer for quiescent publication."""

    def __init__(self, vault_root: Path, manager, *, authority_check: Callable[[], bool],
                 _bootstrap=False):
        self.root = Path(vault_root).resolve()
        self.path = connection.store_path(self.root).resolve()
        self.manager = manager
        self._authority_check = authority_check
        self._handle = None
        self._writer_token = None
        self._identity = None
        self._bootstrap = _bootstrap
        self._acquisition = None
        self._admitted_token = None
        # A producer session resolves takeover/adoption for an unadmitted token;
        # its unresolved outcome (sync pending or divergence) admits reads only.
        self._resolver = None
        self._takeover = None
        # A settlement refusal for the current token, raised again until due (takeover.Refused).
        self._refused = None
        # A producer re-derives custody for each publication, which writes into the vault.
        self._publication_custody = None
        # The producer session that bound this runtime, if any (the in-service owner route).
        self._session = None
        # The last admitted token published its head when it released the lease.
        self._handed_off = False
        self._publisher = None
        if not (_bootstrap and not self.path.exists()):
            # Only a producer adopting a copied vault starts before its live store exists.
            initial = self.sample_head()
            self._identity = (initial.store_id, initial.instance_id)
        with manager._lock:
            if manager._collection_store is not None:
                raise ValueError("the lease manager already owns a collection store runtime")
            manager._collection_store = self

    def record_acquisition(self, record):
        if self._acquisition is None or self._acquisition.fencing_token != record.fencing_token:
            self._acquisition = record
            self._admitted_token = None

    def rebind(self, identity):
        """Serve ``identity``, the ``(store_id, instance_id)`` an owner step made live under the boundary.

        The step replaced the live store or continued it as a new tenure, which the
        identity check of ``sample_head`` would otherwise refuse, and a pending takeover
        verdict described the store before it.
        """
        with self.manager._report_lock:
            self._identity = identity
            self._takeover = self._refused = None

    def record_admission(self, token):
        self._admitted_token = token
        self._takeover = self._refused = None
        self._handed_off = False
        if self._publisher is None:
            self._publisher = _Publisher(self)

    def reader_pool(self, path=None):
        """The cached writer's reader pool when it is open on ``path`` (this runtime's store by default), else None."""
        handle = self._handle
        if handle is None or handle._closed or handle.path != self.path:
            return None
        if path is not None and Path(path) != self.path and Path(path).resolve() != self.path:
            return None
        return handle.readers

    def _meta(self, *keys):
        with connection.store_reader(self.path, self.reader_pool()) as reader:
            found = dict(reader.execute(
                f"SELECT key,value FROM store_meta WHERE key IN ({','.join('?' * len(keys))})", keys))
        return [found.get(key) for key in keys]

    def replica_current(self):
        """Whether the replica already carries the live committed head and lease epoch."""
        head = self.sample_head()
        published, published_epoch, epoch = self._meta(
            schema.META_PUBLISHED_REPLICA_HEAD, schema.META_PUBLISHED_REPLICA_EPOCH,
            schema.META_LEASE_EPOCH,
        )
        return published is not None and published_epoch == epoch and (
            json.loads(published)["commit_seq"], json.loads(published)["head_hash"]
        ) == (head.commit_seq, head.head_hash)

    def replica_matches(self):
        """Whether the vault replica holds exactly the bytes this instance last published.

        An unresolved publication intent owns its target, so it never reads as foreign.
        """
        digest, pending = self._meta(schema.META_LAST_PUBLISHED_REPLICA_SHA256,
                                     schema.META_PENDING_REPLICA_PUBLICATION)
        if pending is not None:
            return True
        try:
            with open(replica.replica_path(self.root), "rb") as file:
                return hashlib.file_digest(file, "sha256").hexdigest() == digest
        except FileNotFoundError:
            return digest is None

    def releases_without_head(self, token):
        """Whether an unadmitted producer token can hand the lease back without a head.

        No store commit can wait on it while this runtime's takeover is unresolved
        (writes refuse) or after its last admitted token published at release, for
        every later token, unless a durable create intent still awaits its cutover.
        """
        from . import authority

        if not (self._bootstrap and token is not None and token != self._admitted_token
                and (self._takeover is not None or self._handed_off)):
            return False
        if not self.path.exists():
            return True
        with connection.head_reader(self.path, self.reader_pool()) as reader:
            intent = authority.pending_create(reader)
        if intent is None:
            return True
        # A fresh reader accepted no work; the migration owner still owes its own publication.
        # A formerly admitted producer or pending create must retain its head handback obligation.
        return (intent["kind"] == authority.MIGRATE and self._publisher is None
                and self._admitted_token is None and self._acquisition is not None
                and self._acquisition.fencing_token == token and self._acquisition.granted)

    def status(self):
        """Operator view of admission; an unresolved takeover or divergence reports attention."""
        token = self.manager._fencing_token
        if token is not None and self.reporting_ready(token):
            divergence, view = self._meta(schema.META_REPLICA_DIVERGENCE, schema.META_VIEW_DIVERGED)
            if divergence is not None or view is not None:
                return {"status": "diverged", "code": "COLLECTION_STORE_DIVERGED", "attention": True,
                        "reason": json.loads(divergence)["reason"] if divergence is not None
                        else "a view carries another store instance's stamp"}
            return {"status": "admitted"}
        return self._takeover.status() if self._takeover is not None else {"status": "unadmitted"}

    def _retire_handle(self):
        """Close the cached writer unless a transaction is open; callers exclude borrowers."""
        handle = self._handle
        if handle is not None:
            if handle.connection.in_transaction:
                return False
            # Quiescent close and the reader pool are the only cross-thread handle operations. The pool
            # has its own lock and lends one reader to one borrower at a time; close leaves a lent
            # reader to close when its lend ends.
            self._handle = None
            self._writer_token = None
            handle.close()
        return True

    def retire_idle_handle(self):
        """Close the cached writer so a direct writer may open, only while nothing borrows it."""
        manager = self.manager
        with manager._report_lock, manager._lock:
            return not (manager._store_borrowers or manager._store_handoff) and self._retire_handle()

    def _refusal(self):
        if self._takeover is not None:
            return self._takeover.error()
        return connection.CollectionStoreError(
            "COLLECTION_STORE_LEASE_REQUIRED", "trusted preview admission is unavailable"
        )

    def reporting_ready(self, token):
        return ((not self._bootstrap or (self._admitted_token is not None and self._admitted_token == token))
                and self._authority_check())

    def report_head(self, token):
        return self.sample_head() if self.reporting_ready(token) else None

    def sample_head(self):
        """Read identity and committed tail in one fresh calling-thread snapshot."""
        from ..writer_lease import CollectionStoreHead

        if self.path != connection.store_path(self.root).resolve():
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_VAULT_MISMATCH", "the live store placement changed"
            )
        # A store an earlier release wrote keeps its head where this reads it; its writer
        # migrates the schema once this head lets the lease manager acquire for the store.
        with connection.head_reader(self.path, self.reader_pool()) as reader:
            reader.execute("BEGIN")
            metadata = legacy_originals.metadata(reader)
            head = CollectionStoreHead(
                metadata[schema.META_STORE_ID], metadata[schema.META_INSTANCE_ID],
                int(metadata[schema.META_COMMIT_SEQ]), metadata.get(schema.META_STORE_HEAD_HASH),
            )
            tail = reader.execute(
                "SELECT commit_seq,store_head_hash FROM txns ORDER BY commit_seq DESC LIMIT 1"
            ).fetchone()
            if (tail or (0, None)) != (head.commit_seq, head.head_hash):
                raise connection.CollectionStoreError(
                    "COLLECTION_STORE_HEAD_INVALID", "committed metadata and tail disagree"
                )
        if self._identity is not None and self._identity != (head.store_id, head.instance_id):
            self._accept_descendant(head, json.loads(metadata[schema.META_LINEAGE]))
        return head

    def _accept_descendant(self, head, lineage):
        """Take the identity an owner adopt-local continued this store under, applied elsewhere.

        The CLI, or the route while this service's lease was idle-released, may continue
        the store as a new instance. It is accepted only with the same store id, a lineage
        that descends from the cached instance and the coordinator recording the new
        instance; custody is then re-verified. Anything else refuses IDENTITY_CHANGED.
        """
        refused = connection.CollectionStoreError(
            "COLLECTION_STORE_IDENTITY_CHANGED", "the enrolled store identity changed"
        )
        store_id, ancestor = self._identity
        parents = {entry["instance_id"]: entry["adopted_from"] for entry in lineage}
        found, seen = head.instance_id, set()
        while found is not None and found != ancestor and found not in seen:
            seen.add(found)
            found = parents.get(found)
        if head.store_id != store_id or found != ancestor or not self.manager.config.enabled:
            raise refused
        recorded = self.manager.client.status().collection_store_head
        if recorded is None or (recorded.store_id, recorded.instance_id) != (head.store_id, head.instance_id):
            raise refused
        if self._session is not None and not self._session.verify_custody():
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_CUSTODY_UNVERIFIED", "single-host custody no longer verifies"
            )
        self._identity = (head.store_id, head.instance_id)

    def _admit(self, token, *, reads=False):
        from .takeover import SCHEMA_PENDING

        if self.manager.config.enabled:
            self.manager.validate_fencing_token(token)
        # Reads of an unadmitted copy continue, except while its migration fails: the
        # refusal then names the failure and its retry, and reads never repeat the attempt.
        if not self.reporting_ready(token) and not (
            reads and self._takeover is not None and self._takeover.code != SCHEMA_PENDING and self.path.exists()
        ):
            raise self._refusal()
        self.sample_head()

    def _record_schema(self, token, version, **authority):
        """Record what a store at ``version`` requires, around this runtime's migration of an earlier store."""
        from .. import state_migration

        state_migration.record_collection_store_schema(
            self.root, version=version, authority_check=lambda: self._authority(token, **authority))

    def _authority(self, token, *, progress=False, reads=False):
        if progress:
            self.manager._renew_collection_store(token, due_only=True)
        self._admit(token, reads=reads)
        return self.manager._mutation_coordinator_for(self.root).current_thread_holds_boundary()

    @contextmanager
    def checkout(self):
        from ..cli_ops import OpError
        from . import capability

        capability.require_not_disabled(self.root)
        if self._bootstrap and not self.reporting_ready(self.manager._fencing_token):
            if self._resolver is None:
                raise connection.CollectionStoreError(
                    "COLLECTION_STORE_LEASE_REQUIRED", "isolated recovery has not admitted this token"
                )
            # Takeover/adoption, rechecked every 10 s or on replica change; an unresolved
            # takeover leaves reads of the local copy, and never closes a borrowed handle.
            self._resolver()
        with self.manager._collection_store_checkout():
            # A rollback between the first check and borrowing must also refuse this checkout.
            capability.require_not_disabled(self.root)
            with self.manager._lock:
                token = self.manager._fencing_token
            if token is not None:
                try:
                    # Admission checks coordinator expiry and revocation without renewing write activity.
                    self._admit(token, reads=True)
                except OpError as error:
                    if error.code != "WRITER_FENCED":
                        raise
                    token = None
            if token is None:
                # Reacquire only before lending the handle; in-flight authority checks never reacquire.
                token = self.manager.ensure_writer(cause="read").fencing_token
                self._admit(token, reads=True)
            if self._handle is None or self._writer_token != token:
                with self.manager.consistency_guard(self.root, operation="collection_store_open"):
                    with self.manager._lock:
                        if self._handle is not None and self._writer_token != token:
                            if (self.manager._store_borrowers != {threading.get_ident(): 1}
                                    or not self._retire_handle()):
                                raise connection.CollectionStoreError(
                                    "COLLECTION_STORE_BUSY", "the stale writer is still borrowed"
                                )
                        if self._handle is None:
                            self.sample_head()
                            # Opening may serve reads of an unadmitted copy; transactions never.
                            # It migrates a copy an earlier release wrote, after recording the
                            # new schema's IDs as admission's writer does, so reads continue. That
                            # can precede a refusal of the pending takeover; the launcher's store
                            # schema check refuses an earlier release afterwards, as after any migration.
                            opening = [True]
                            self._handle = connection.open_writer(
                                self.path, lease_check=lambda: self._authority(
                                    token, reads=opening[0]),
                                schema_guard=lambda version: self._record_schema(
                                    token, version, reads=True),
                            )
                            opening[0] = False
                            self._writer_token = token
            handle, changed = self._handle, False
            before = handle.connection.total_changes
            try:
                with borrowed_writer(self.root, handle, self.manager) as writer:
                    try:
                        yield writer
                    finally:
                        # Read while still a borrower: the handle cannot be retired yet, unless
                        # this borrower lent the store to its producer (``StoreServer.lent``).
                        changed = not handle._closed and handle.connection.total_changes != before
            finally:
                if changed and self._publisher is not None:
                    self._publisher.note_commit()

    @contextmanager
    def _publication_writer(self, token, *, borrow_cached=False):
        """This store's writer for a publication step; the caller holds the boundary."""
        if not self._authority(token, progress=True):
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_LEASE_REQUIRED", "publication authority is unavailable"
            )
        if self._publication_custody is not None and not self._publication_custody(token):
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_CUSTODY_UNVERIFIED", "single-host custody no longer verifies; nothing was published"
            )
        handle = self._handle if borrow_cached else None
        if (handle is not None and not handle._closed and handle.path == self.path
                and self._writer_token == token):
            with self.manager._lock:
                exclusive = (self.manager._store_publishing and self.manager._store_handoff
                             and not self.manager._store_borrowers and not self.manager._active_mutations)
            if not exclusive or handle.connection.in_transaction or handle.core.in_transaction():
                raise connection.CollectionStoreError(
                    "COLLECTION_STORE_BUSY", "the cached writer is not idle inside a publication window"
                )
            # A3: exclusive publication borrows the same connection, retaining its change witness and warm cache.
            owner, reusable = handle._owner_thread, False
            handle._owner_thread = threading.get_ident()
            try:
                yield handle
                reusable = not (handle._closed or handle.connection.in_transaction or handle.core.in_transaction())
                if not reusable:
                    raise connection.CollectionStoreError(
                        "COLLECTION_STORE_BUSY", "the publication writer did not return idle"
                    )
            finally:
                try:
                    if not reusable:
                        self._handle = self._writer_token = None
                        handle.close()
                finally:
                    handle._owner_thread = owner
            return
        if not self._retire_handle():
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_BUSY", "the retired writer still has a transaction"
            )
        with connection.open_writer(self.path, lease_check=lambda: self._authority(token, progress=True),
                                    schema_guard=lambda version: self._record_schema(token, version, progress=True)) as writer:
            yield writer

    def publish(self, token, *, deadline):
        """Coalesced publication (A9): verify outside exclusion and revalidate before effects.

        Each boundary step is brief and taken at an idle instant. Each resumed step first
        renews the lease with the coordinator, so a host that lost it while copying
        abandons the swap. Returns the published head (possibly older than the live
        one), or None when the store was busy before anything was recorded.
        """
        from ..cli_ops import OpError
        from ..writer_lease import CollectionStoreHead

        manager = self.manager

        @contextmanager
        def step(patience, cancelled):
            if patience:
                try:
                    manager._renew_collection_store(token)
                except OpError as error:
                    raise connection.CollectionStoreError(
                        "COLLECTION_STORE_LEASE_REQUIRED", "the writer lease was lost during the copy"
                    ) from error
            with manager._store_publication_window(token, patience=patience, cancelled=cancelled):
                with self._publication_writer(token, borrow_cached=True) as writer:
                    yield writer, lambda: self._authority(token, progress=True)

        try:
            result = replica.publish_replica_concurrently(
                self.root, step=step, deadline=deadline, patience=manager._mutation_timeout_seconds,
                cancelled=lambda: manager._fencing_token != token or manager._store_closed,
            )
        except connection.CollectionStoreError as error:
            if error.code == "COLLECTION_STORE_BUSY":
                return None
            raise
        if result.status != "published":
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_FLUSH_PENDING", result.reason or result.status
            )
        head = self.sample_head()
        return CollectionStoreHead(head.store_id, head.instance_id, result.commit_seq, result.head_hash)

    def _drain_views(self, handle, *, deadline, cancelled=None):
        """Install the views that committed changes left pending, before a handoff or a stop (design §5).

        It publishes what the serving store thread would, under the release that lets that
        thread publish, through the same reconcile. Its writer is fresh, so it runs no census: an
        item view missing from a path its bytes may have reached waits for the next holder's census.
        Half of the time left sets a cooperative cutoff for starting optional work; in-flight
        filesystem calls and required finalization still finish. Unfinished views stay pending
        for the next holder's startup pass.
        """
        from . import capability

        if not capability.views_published(self.root):
            return
        from ..governance import principal
        from .writer import CollectionWriter

        now = time.monotonic()
        deadline = now + max(0.0, deadline - now) * DRAIN_SHARE

        def stopped():
            return time.monotonic() >= deadline or (cancelled is not None and cancelled())

        writer, after, passes = CollectionWriter(self.root, handle), None, 0
        try:
            with principal.library_scope():
                # One pass installs each view; the next confirms it current. Each pass is one window.
                while passes < 2 and not stopped():
                    if handle.connection.execute(
                            "SELECT 1 FROM projection_state WHERE state='pending' LIMIT 1").fetchone() is None:
                        return
                    after = writer.reconcile_views(
                        limit=VIEW_BATCH_PATHS, after=after, pending_only=True, stop=stopped,
                        missing_ready=(lambda cid: False)
                        if capability.collection_views_enabled(self.root) else None)["next_path"]
                    passes += after is None
        except Exception:  # noqa: BLE001 - the next holder's startup pass installs what is left
            logger.warning("pending collection views were not installed before the handoff", exc_info=True)

    def flush(self, token, *, deadline, cancelled=None, drain_views=True):
        """Called only after admission stops, drainage and boundary acquisition."""
        with self._publication_writer(token) as writer:
            if drain_views:
                self._drain_views(writer, deadline=deadline, cancelled=cancelled)
            result = replica.publish_replica(
                self.root, writer, authority_check=lambda: self._authority(token, progress=True),
                deadline=deadline, cancelled=cancelled,
            )
            if result.status != "published":
                raise connection.CollectionStoreError(
                    "COLLECTION_STORE_FLUSH_PENDING", result.reason or result.status
                )
            head = self.sample_head()
            if (head.commit_seq, head.head_hash) != (result.commit_seq, result.head_hash):
                raise connection.CollectionStoreError(
                    "COLLECTION_STORE_FLUSH_PENDING", "publication did not reach the committed head"
                )
            # Every orderly boundary also leaves the store file alone holding this head. A
            # reader mid-transaction can hold that back; the replica already carries the head,
            # so the flush stands and the WAL keeps the commits.
            if not writer.checkpoint():
                logger.warning("collection store WAL checkpoint was held back by an open reader")
            return head


# --- the service's store thread (S1) ----------------------------------------------------

#: The facades whose collection selectors the vault's marker can route to the store.
_STORE_COMMANDS = frozenset({"record_memory", "plan_memory"})
#: The commands that carry a curation or episode Records step, or structured-files maintenance. They go
#: to the store thread only when a step they run or seal, or the maintenance, writes a collection the
#: marker routes to the store (``route``).
# nosemgrep: ep-word-set -- the two command names the command registry fixes for curation steps.
_CURATION_COMMANDS = frozenset({"maintain_memory", "episode_memory"})
#: How long the idle store thread waits for a request before it looks for import jobs again.
IMPORT_IDLE_SECONDS = 1.0
#: How long it leaves running jobs that advanced nothing (blocked or refused) alone.
IMPORT_RETRY_SECONDS = 30.0
#: First retry of a missing derived source; repeated stops back off to IMPORT_RETRY_SECONDS.
DERIVED_RETRY_SECONDS = 2.0
#: Rows per derived backfill batch; each tick runs one batch for one building collection.
BACKFILL_BATCH_ROWS = 128
#: Paths per pending-view publication window: one collection's summary pages at their bounds.
VIEW_BATCH_PATHS = 16
#: Longest run of edit-back windows in one checkout: it bounds how long that checkout's admission
#: is reused and how long a publication waits for an idle instant.
VIEW_SLICE_SECONDS = 0.05
#: Quiet a tick needs before it starts. A call in flight already shuts the gate (its foreground mark,
#: then ``_served_at``), so the gap only parts back-to-back calls; it stays under a 50 ms client poll.
FOREGROUND_GRACE_SECONDS = 0.02
#: Under continuous demand a tick with work due still runs every 2 s, so due edit-back keeps bounded turns.
MAINTENANCE_DEFER_SECONDS = 2.0
#: A views tick with no view work due only checks history pages' release inputs; under continuous
#: demand that check runs this often, which keeps its cost off most reads and still bounds its delay.
VIEWS_POLL_DEFER_SECONDS = 10.0
# Bound watcher metadata independently of collection size; overflow requests a later census.
_VIEW_CHANGE_CAPACITY = 1024
#: How often it retries a vault whose store it could not open.
OPEN_RETRY_SECONDS = 30.0
_SERVERS: dict[Path, StoreServer] = {}
_SERVED: dict[Path, object] = {}  # vault root -> the lease manager of the service serving it
_SERVERS_LOCK = threading.Lock()


def serve(vault_root, manager):
    """Serve a vault's collection store from this service, through its own store thread.

    Called at service startup and standby promotion. A vault that routes nothing to a
    store starts its thread at its first store-routed request instead.
    """
    from . import ownership

    root = Path(vault_root).resolve()
    with _SERVERS_LOCK:
        _SERVED[root] = manager
    if not ownership.file_only(root):
        _server(root)


def route(command, vault_root, arguments):
    """The store thread a request must run on, or None when file collections answer it.

    A selector the vault's marker routes to the store, a NEW summary collection's create,
    Records discovery, and a curation or episode command that runs a Records step or
    structured-files maintenance on a store collection run on the store thread of the service serving this vault;
    a caller that bound its own writer keeps it. Without that service a routed selector
    refuses (C never falls back to its views), a summary create on a vault with no store
    refuses as the owner's enrolment step, and the inventory names C as unreadable.
    """
    from ..cli_ops import OpError
    from .preview import UNSERVED, bound_writer

    root = Path(vault_root).resolve()
    if bound_writer(root) is not None:
        return None
    if command in _CURATION_COMMANDS:
        return _curation_route(command, root, arguments)
    if command not in _STORE_COMMANDS:
        return None
    from . import ownership

    selector = arguments.get("collection", arguments.get("manifest_path"))
    proof = ownership.current(root)
    summary = command == "record_memory" and arguments.get("action") == "create" and summary_create(root, arguments)
    if proof.marker is not None or summary:
        # Only a vault with a store, or a summary create that may enrol one, pays for the store libraries.
        from . import connection

        connection.load_store_libraries()
    if summary:
        if proof.marker is None:
            from . import admission, capability

            capability.require_records_summary(root)
            raise admission.enrollment_required()
    elif selector is None and (
        arguments.get("action") == "inspect"
        or (command == "record_memory" and arguments.get("action") == "describe")
    ):
        # Both public store tools need the service writer for global inventory.
        server = None if proof.marker is None else _server(root)
        return server if server is not None and server.runtime is not None else None
    elif selector is None or (proof.selected(root, selector) is None and not _creating(root, selector)):
        return None
    server = _server(root)
    if server is None and not summary:
        return None  # Collection resolution decides visibility before reporting an unavailable store.
    if server is None:
        raise OpError(
            "COLLECTION_STORE_UNAVAILABLE", UNSERVED, "Send the request to the service that serves this vault.",
        )
    return server


def _curation_route(command, root, arguments):
    """The store thread for a curation or episode command that runs a Records step on a store collection,
    or for structured-files maintenance on one.

    A step on a collection the store owns commits through the store writer, which only the store
    thread holds, so the whole command runs there. Every other curation, in a vault with a store
    or without one, keeps its path. With no service or an unavailable canonical ownership proof,
    nothing is routed. The step's own resolution then refuses and alone decides whether this caller
    may hear that the collection exists. Raising here would tell every caller first.
    """
    from ..cli_ops import OpError
    from . import curation_step

    try:
        if not curation_step.needs_store(root, command, arguments):
            return None
    except (connection.CollectionStoreError, OpError):
        return None
    connection.load_store_libraries()
    return _server(root)


def projection_route(vault_root, path):
    """Route only canonically owned paths; a view never substitutes for an unavailable store."""
    import os

    from ..cli_ops import OpError
    from . import ownership
    from .preview import bound_writer

    root = Path(vault_root)
    if bound_writer(root) is not None:
        return None
    # Resolve Windows root junctions before no-follow anchoring.
    if os.name == "nt" or root.is_symlink():
        root = root.resolve()
    if not ownership.relevant(path):
        return None
    canonical = state_paths.resolved_vault_path(root, expanduser=False)
    if canonical != root:
        # Bind marker bytes to the canonical root, never to the earlier path spelling.
        # A root that is already canonical was just read there: recall routes every hit.
        root = canonical
    entry = ownership.owned(root, path)
    if entry is None:
        return None
    server = _server(root)
    if server is None:
        raise OpError("COLLECTION_STORE_UNAVAILABLE", "canonical projections require the serving collection store")
    return server

def _durable_create(root):
    """The marker entries a pending intent left on disk, or None when there is none or it cannot be read.

    File collections route through here too, so a store that cannot be read must leave
    them on their file route rather than refuse them.
    """
    from . import authority

    try:
        if not connection.store_path(root).exists():
            return None
        with closing(connection.open_head_reader(connection.store_path(root))) as reader:
            intent = authority.pending_create(reader)
    # The head reader denies other columns as sqlite3.Error, so a read past store_meta would look like no intent here.
    except (connection.CollectionStoreError, sqlite3.Error, OSError):
        return None
    return None if intent is None else [{"collection_id": entry["collection_id"], "manifest_path": entry["manifest_path"]}
                                        for entry in intent["collections"]]


def _creating(root, selector):
    """Whether ``selector`` names the collection a served create left pending.

    The marker routes it only once the create finishes, so until then its requests go to
    the store thread, which refuses them as busy, not as a collection that does not exist.
    """
    from . import authority

    server = _SERVERS.get(root)
    pending = None if server is None else server.pending_create
    if pending is None and server is not None and server.session is None:
        # Not open yet, as after a restart: a create left pending is only on disk so far.
        pending = _durable_create(root)
    return pending is not None and authority.selected_entry(root, {"collections": pending}, selector) is not None


def summary_create(root, arguments):
    """Whether a create's manifest is a NEW summary collection (records-summary-v1).

    Only classifies: a manifest that does not parse keeps its existing route, whose own
    validation reports it.
    """
    from .. import structured_collections as collections

    path, text = arguments.get("manifest_path"), arguments.get("manifest_text")
    if not isinstance(path, str) or not isinstance(text, str):
        return False
    try:
        return collections.parse_manifest_bytes(Path(root), path, text.encode()).view_mode == "summary"
    except Exception:  # noqa: BLE001 - classification only
        return False


def served_create(vault_root, arguments):
    """Create a NEW summary collection on the serving session, or None for any other manifest.

    It runs on the store thread under the request principal, through the producer's own
    create (``admission.create_new``), which a vault already enrolled in this store
    completes with writer authority. Enrolling a vault is the owner's offline step.
    """
    from .. import structured_collections as collections
    from .. import writer_lease
    from . import admission, capability

    root = Path(vault_root).resolve()
    if not summary_create(root, arguments):
        return None
    capability.require_records_summary(root)
    server = _SERVERS.get(root)
    if server is None or server.session is None or server.thread is not threading.current_thread():
        raise connection.CollectionStoreError(
            "COLLECTION_STORE_UNAVAILABLE", "only the service serving this vault creates a summary collection")
    session = server.session
    fence = session.fence_client.collection_store_fence()
    if not fence.enrolled:
        # Served, so this vault's store was enrolled: its coordinator was replaced, or retired the fence.
        raise admission.unenrolled(fence)
    with server.lent() as lend:
        result = admission.create_new(
            session, server.manager, arguments["manifest_path"], arguments["manifest_text"],
            why=arguments["why"], request_id=writer_lease.active_mutation_request_id(),
            fence_client=session.fence_client, scaffold=arguments.get("scaffold", True))
        if result.get("status") == "pending":
            # Committed, but its replica publication did not finish: the store stays the
            # create's until this thread's create tick resumes it, so this request ends
            # without a fresh checkout and says so.
            lend.resume = False
            server.pending_create = [{"collection_id": result["collection_id"], "manifest_path":
                                      collections._reference_key(root, arguments["manifest_path"])}]
            result = {**result, "warnings": [*result.get("warnings", ()), (
                f"collection_publication_pending ({result.get('reason')}): the collection is created and "
                "the service is finishing its publication; until then store requests refuse as "
                "COLLECTION_STORE_BUSY, so retry them shortly")]}
        return result


def _closed(manager):
    return manager._stop.is_set() or manager._store_closed


def _serving_manager(root):
    """The lease manager of the running service serving ``root``; a closed service's entry is dropped.

    The caller holds ``_SERVERS_LOCK``.
    """
    manager = _SERVED.get(root)
    if manager is not None and _closed(manager):
        del _SERVED[root]
        return None
    return manager


def served(root):
    """Whether a running service in this process serves ``root``."""
    with _SERVERS_LOCK:
        return _serving_manager(Path(root).resolve()) is not None


def _server(root):
    with _SERVERS_LOCK:
        server, manager = _SERVERS.get(root), _serving_manager(root)
        if server is None and manager is not None:
            server = _SERVERS[root] = StoreServer(root, manager)
        return server


def notify_view_change(root, path, *, destination=None, directory=False):
    """Wake an existing store thread with a filesystem hint, without opening or calling it."""
    watched_root = Path(root)
    root = watched_root.resolve()

    def relative(value):
        candidate = Path(value)
        if not candidate.is_absolute():
            return candidate.as_posix()
        try:
            return candidate.relative_to(watched_root).as_posix()
        except ValueError:
            try:
                return candidate.relative_to(root).as_posix()
            except ValueError:
                return str(candidate)  # A move outside the vault retains its destination hint.

    change = (relative(path), None if destination is None else relative(destination), directory)
    with _SERVERS_LOCK:
        server = _SERVERS.get(root)
    if server is None:
        return
    with server._condition:
        if server._closed:
            return
        if change in server._view_changes or len(server._view_changes) < _VIEW_CHANGE_CAPACITY:
            server._view_changes[change] = False
        else:
            server._view_census_needed = True
        server._condition.notify_all()


class _Call:
    """One request handed to the store thread, run in a copy of the caller's context."""

    def __init__(self, work, *, wake_maintenance=True, read_only=False):
        import contextvars

        self.context = contextvars.copy_context()
        self.work = work
        self.wake_maintenance = wake_maintenance
        self.read_only = read_only
        self.taken = False
        self.done = threading.Event()
        self.result = self.error = None


class StoreServer:
    """The service's store thread for one store-routed vault (S1).

    It opens the production session (custody, lease, coordinator fence and takeover),
    which registers the runtime in ``_SERVING``, and so owns the store writer. Every
    store-routed request runs on it in turn, with the caller's context and principal and
    the writer bound. Between requests, while this release enables records-summary-v1,
    it finishes a create whose publication is pending and advances running import jobs
    one batch at a time; each batch runs under its job's own bound principal, never the
    service's. As the in-process owner, it also advances building projections and rollups
    and publishes pending views, one bounded batch at a time. Between two requests it
    runs one tick, the one that has waited longest, once foreground work has been quiet for
    ``FOREGROUND_GRACE_SECONDS``; a tick that has starved (``_starved``) runs regardless, so
    continuous demand still leaves it a turn. A request it cannot take within the
    mutation timeout refuses as retryable, so a running batch never holds an
    acknowledgement past that timeout. A vault it cannot open refuses store-routed
    requests with the reason and retries; file collections never wait on it.
    """

    def __init__(self, root, manager):
        self.root, self.manager = root, manager
        self.session = self.runtime = None
        self.refusal = ("COLLECTION_STORE_UNAVAILABLE", "the collection store is opening")
        self._stack = self._scope = None
        self._retry_at = 0.0
        # When each maintenance tick is next due; between two requests the most overdue one runs.
        self._ticks = {"create": 0.0, "import": 0.0, "backfill": 0.0, "views": 0.0, "derived": 0.0}
        self._served_at = 0.0  # when the store thread last finished a request
        self._derived_handle = None  # the writer handle whose derived file the last derived tick reconciled
        self._derived_pass = None  # the running rebuild's bulk pass budget, across its steps
        self._derived_retry = DERIVED_RETRY_SECONDS
        self._views_after = None  # where the current pass over pending views continues
        self._views_passes = 0  # completed passes since the last maintenance wake
        self._views_owed = False  # a publication pass that a wake or a yielding tick left unstarted
        self._edit_back = None  # this store's edit-back settle state, made at its first views tick
        self._edit_handle = None
        # The store, instance, release inputs and head whose history pages the drain has queued.
        self._history_policy = None
        # A views tick after an idle release looks for edited view files without the lease: where its
        # sweep continues (None between sweeps), when the next sweep is due, the changes it found that
        # a leased pass has not yet run on, and the ones a leased pass ran on (path to what the sweep
        # saw), so a view that stays changed wakes the tick once, and one whose wake failed wakes it again.
        self._sweep_after = None
        self._sweep_due = 0.0
        self._sweep_pending = {}
        self._sweep_seen = {}
        # False waits for exact-path lookup; True retains an unhandled hint for the census.
        self._view_changes = {}
        self._view_census_needed = False
        self._view_retry_at = 0.0
        self._backfill_refused = {}  # collection id -> when its refused backfill is retried
        self._backfill_last = ""  # the collection the last batch advanced; the next one follows it
        self.pending_create = None  # the marker entries a served create left pending, until it finishes
        self._requests = deque()
        self._closed = False
        self._condition = threading.Condition()
        self.thread = threading.Thread(target=self._run, name="exomem-collection-store", daemon=True)
        self.thread.start()

    def call(self, work, *, wake_maintenance=True, read_only=False, extend=None, on_refusal=None):
        """Run ``work`` on the store thread and return its result or raise its error.

        ``read_only`` says the work changes nothing, so it runs under one view of path
        placement, host configuration and governance policy (``_serve``). ``extend`` lets a
        write that is still waiting at its acceptance deadline keep its place: it takes the
        completion deadline and returns a later acceptance deadline, or None to refuse.
        ``on_refusal(error, waited_seconds)`` hears a write refusal this queue makes
        itself, just before the caller gets it: a timeout in the queue, or accepted work
        still running. A refusal the work or ``extend`` raises reaches the caller from there."""
        from ..cli_ops import OpError
        from ..writer_lease import acknowledgement_budget_deadline

        entry = time.monotonic()
        completion_deadline = acknowledgement_budget_deadline(entry)
        deadline = min(completion_deadline, entry + self.manager._mutation_timeout_seconds)
        # A store handoff, such as an idle release, refuses every checkout until it ends. Wait it out
        # here, where this thread holds nothing: a waiter at the deep refusals could stall the handoff.
        # The wait ends at the acceptance deadline, which the queue wait below shares.
        with foreground_priority.foreground(), foreground_priority.awaiting_bulk(), \
                call_spans.span("store.admission_wait"):
            self.manager.await_store_admission(deadline)
        def refuse(error):
            if on_refusal is not None:
                on_refusal(error, time.monotonic() - entry)
            return error

        request = _Call(work, wake_maintenance=wake_maintenance, read_only=read_only)
        with self._condition:
            if not self._closed:
                self._requests.append(request)
                self._condition.notify_all()
        # Between two rebuild steps this thread waits as a bulk pass while other requests run;
        # a request waiting to be taken waits on it, so it releases the pass (``_derived_tick``).
        refusal = None
        with foreground_priority.foreground(), foreground_priority.awaiting_bulk(), self._condition, \
                call_spans.span("store.queue_wait"):
            while not request.taken:
                remaining = deadline - time.monotonic()
                if remaining <= 0 and not self._closed and extend is not None:
                    # Work the store has not taken never ran, so waiting longer cannot repeat it.
                    try:
                        later = extend(completion_deadline)
                    except BaseException:
                        # A caller that sees an error must leave nothing queued to run later.
                        if request in self._requests:
                            self._requests.remove(request)
                        raise
                    if later is not None and later > deadline:
                        deadline = later
                        continue
                if remaining <= 0 or self._closed:
                    if request in self._requests:
                        self._requests.remove(request)
                    # Built here, where the queue is stable; journaled below, off the lock the store thread takes.
                    refusal = self._busy_refusal(
                        "the collection store did not take this request within the mutation timeout")
                    break
                self._condition.wait(remaining)
        if refusal is not None:
            raise refuse(refusal)
        # The worker owns accepted work's priority; a waiting caller must not park its bulk dependencies.
        waiting = (foreground_priority.awaiting_bulk if foreground_priority._holds_a_boundary()
                   else foreground_priority.outside_foreground)
        with waiting(), call_spans.span("store.serve"):
            completed = request.done.wait(max(0.0, completion_deadline - time.monotonic()))
        if not completed and read_only:
            # A read changes nothing, so a missed deadline is a retryable refusal, not an uncertain commit.
            raise self._busy_refusal(
                "the collection store did not finish this read within its completion deadline")
        if not completed:
            # Accepted work survives the wait deadline so its outcome and replay remain owned.
            raise refuse(OpError(
                "MUTATION_ACKNOWLEDGEMENT_PENDING",
                "the accepted collection-store operation is still executing and may still commit",
                details={"status": "uncertain", "committed": None},
            ))
        if request.error is not None:
            raise request.error
        return request.result

    def _busy_refusal(self, message):
        """The retryable refusal for a request this store thread did not serve in time."""
        error = connection.busy(message)
        try:
            error.details["store_thread"] = self._queue_refusal_state()
        except Exception:  # noqa: BLE001 - optional diagnostics must not replace the original refusal
            pass
        return error

    def _queue_refusal_state(self):
        """Observe the worker at refusal, without paths, source text or frame locals."""
        import sys

        from ..stack_sites import package_frames

        state = {"alive": self.thread.is_alive(), "queued_requests": len(self._requests),
                 "stack_status": "unavailable", "frames": [], "frames_truncated": None}
        frame = sys._current_frames().get(self.thread.ident)
        if frame is None or not self.thread.is_alive():
            return state
        # Bound the diagnostic walk; only shipped package symbols leave this process.
        frames, truncated = package_frames(frame)
        state.update(stack_status="captured", frames=frames, frames_truncated=truncated)
        return state

    def _stopping(self):
        return _closed(self.manager)

    def _run(self):
        try:
            while not self._stopping():
                # Routing demand can precede queue insertion; yield outside every checkout before checking the queue.
                # The wait ends where the most overdue tick starves, so the starvation bound holds under demand.
                foreground_priority.yield_to_foreground(max_wait=self._yield_allowance(time.monotonic()))
                now = time.monotonic()
                if self.runtime is None and now >= self._retry_at:
                    self._open()
                if self._view_work_due(now):
                    self._ticks["views"] = min(self._ticks["views"], now)
                name = self._next_tick(now)
                request = self._take(self._idle_wait(name, now))
                if request is not None:
                    self._serve(request)
                    served = self._served_at = time.monotonic()
                    if request.wake_maintenance:
                        # The request may have started a job, revised a declaration or left views pending.
                        # A tick already overdue keeps its age: a stream of writes must not restart its starvation clock.
                        self._ticks = {tick: min(due, served) for tick, due in self._ticks.items()}
                        # The wake's publication pass is views work until a tick starts it, even if no request ever pauses.
                        self._views_after, self._views_passes, self._views_owed = None, 0, True
                    if self._starved("views", served):
                        # Saved views get one bounded turn per starvation bound even when requests never stop.
                        self._ticks["views"] = time.monotonic() + self._views_tick()
                elif self._tick_may_start(name, time.monotonic()):
                    # One tick per turn, so a waiting request is taken between any two.
                    self._ticks[name] = time.monotonic() + getattr(self, f"_{name}_tick")()
        finally:
            self._close()

    def _starves_at(self, name, now):
        """When a tick starves: ``MAINTENANCE_DEFER_SECONDS`` after it fell due, or, for a views tick
        with no view work due, which only checks history pages' release inputs, ``VIEWS_POLL_DEFER_SECONDS``."""
        polling = name == "views" and not self._view_work_due(now)
        return self._ticks[name] + (VIEWS_POLL_DEFER_SECONDS if polling else MAINTENANCE_DEFER_SECONDS)

    def _starved(self, name, now):
        """Whether a due tick has waited out its starvation bound without a quiet gap to run in."""
        return now >= self._starves_at(name, now)

    def _next_tick(self, now):
        """The tick the gate is asked about: the most overdue starved one, else the most overdue.

        A views poll starves later than other ticks, so the most overdue tick need not be the one starved.
        """
        starved = [name for name in self._ticks if self._starved(name, now)]
        return min(starved or self._ticks, key=self._ticks.get)

    def _tick_may_start(self, name, now):
        """Whether the gate admits a due tick: foreground work has been quiet, or the tick starved.

        It runs outside every checkout and boundary, and no foreground thread waits on it. Quiet follows
        ``foreground_priority.bulk_free_since``, so a thread that awaits bulk work opens it after the gap,
        and the gap also counts from the end of this thread's own last request. Here a lease handoff
        holds back even a starved tick, so the handoff drains the store first. The starved views turn
        after a request (``_run``) does not ask about a handoff, as the views turn there never did.
        """
        if now < self._ticks[name]:
            return False
        if self._starved(name, now) and not self.manager._store_handoff:
            return True
        free_since = foreground_priority.bulk_free_since()
        return (free_since is not None and not self._aside()
                and now - max(free_since, self._served_at) >= FOREGROUND_GRACE_SECONDS)

    def _idle_wait(self, name, now):
        """Seconds to wait for a request before the next look at the tick gate."""
        due = self._ticks[name]
        if due > now:
            return min(due, now + IMPORT_IDLE_SECONDS) - now
        if self._tick_may_start(name, now):
            return 0.0
        # Closed: look again when the quiet gap ends. While foreground work holds bulk work, or when the
        # gap has ended and the gate is still shut (a handoff, a writer at the boundary), look again after
        # one gap, or this loop would spin. The loop's yield (``_yield_allowance``) ends at starvation.
        free_since = foreground_priority.bulk_free_since()
        quiet_in = (FOREGROUND_GRACE_SECONDS if free_since is None
                    else max(free_since, self._served_at) + FOREGROUND_GRACE_SECONDS - now)
        return quiet_in if quiet_in > 0 else FOREGROUND_GRACE_SECONDS

    def _yield_allowance(self, now):
        """How long the loop may wait for foreground work to end: until the first tick starves."""
        starves = min(self._starves_at(name, now) for name in self._ticks) - now
        return max(0.0, min(foreground_priority.MAX_YIELD_SECONDS, starves))

    def _take(self, timeout):
        with self._condition:
            if not self._requests and timeout > 0:
                self._condition.wait(timeout)
            if not self._requests:
                return None
            request = self._requests.popleft()
            request.taken = True
            self._condition.notify_all()
            return request

    def _view_work_due(self, now):
        if now < self._view_retry_at:
            return False
        with self._condition:
            notified = any(not checked for checked in self._view_changes.values())
        return notified or (self._ticks["views"] <= now and (
            self._views_owed or self._views_after is not None or self._views_passes == 1
            or self._edit_back is not None and self._edit_back.wait(now) <= 0
        ))

    def take_view_hints(self):
        """Transfer retained path/move/directory hints and the overflow flag to the store's census.

        The caller takes responsibility for these hints. A watcher never invokes this or expands
        directories. Both checked and unchecked hints share the one bounded mailbox until taken.
        """
        with self._condition:
            hints, census = tuple(self._view_changes), self._view_census_needed
            self._view_changes.clear()
            self._view_census_needed = False
        return hints, census

    def _open(self):
        from ..cli_ops import OpError
        from . import admission

        stack = ExitStack()
        try:
            session = stack.enter_context(admission.production_session(self.root))
            # The writer credential reads the coordinator's fence; moving it is the operator's.
            fence_client = self.manager.client
            if fence_client is None:
                raise admission.coordination_required("serving the collection store")
            fence = fence_client.collection_store_fence()
            if not fence.enrolled:
                # A copied vault, or a replaced coordinator, that never enrolled this store: the
                # fence cut is the owner's offline step, which the writer credential cannot make.
                # A retired fence means this host's marker predates the vault's export to files.
                raise admission.unenrolled(fence)
            # A create left pending before a restart: its requests are busy, not missing.
            self.pending_create = _durable_create(self.root)
            try:
                admission.open_store(session, self.manager, fence_client=fence_client)
            except (OpError, connection.CollectionStoreError):
                if self.manager._collection_store is None:
                    raise
                # Bound and registered: its checkout resolves the takeover once the lease allows.
                logger.info("collection store takeover is pending", exc_info=True)
        except Exception as error:  # noqa: BLE001 - store-routed requests refuse with the reason
            stack.close()
            code = getattr(error, "code", "COLLECTION_STORE_UNAVAILABLE")
            # Store errors read "CODE: message"; the refusal adds the code once.
            self.refusal = (code, str(error).removeprefix(f"{code}: "))
            self._retry_at = time.monotonic() + OPEN_RETRY_SECONDS
            logger.warning("the collection store could not be served; retrying", exc_info=True)
            return
        self._stack, self.session, self.runtime = stack, session, self.manager._collection_store

    def _serve(self, request):
        from ..cli_ops import OpError
        from ..governance import policy
        from .preview import preview_store

        def bound():
            with ExitStack() as scope:
                if request.read_only:
                    # Opened here, inside the work, so each view lives exactly as long as the work
                    # does: a caller that gets MUTATION_ACKNOWLEDGEMENT_PENDING returns while this
                    # still runs. Governance policy stays pinned through the release, which a read's
                    # snapshot allows. Path resolution and the connector ceiling are pinned for the
                    # body only: the terminal egress filter re-reads them live. A write opens neither: it can
                    # wait for the mutation boundary and must then decide on the policy and ceiling
                    # it finds, not on the ones it read before the wait.
                    scope.enter_context(state_paths.resolution_scope())
                    scope.enter_context(policy.request_policy_snapshot())
                scope.enter_context(preview_store(self.root, self.runtime))
                self._scope = scope
                try:
                    return request.work()
                finally:
                    self._scope = None

        try:
            if self.runtime is None:
                code, message = self.refusal
                raise OpError(code, message, "Retry once the service serves the collection store.")
            request.result = request.context.run(bound)
        except BaseException as error:  # noqa: BLE001 - raised again on the caller's thread
            if isinstance(error, connection.CollectionStoreError) and error.code == "COLLECTION_STORE_BUSY":
                error = connection.busy(str(error).removeprefix("COLLECTION_STORE_BUSY: "))
            request.error = error
        finally:
            request.done.set()

    @contextmanager
    def lent(self):
        """Step the current request's checkout aside while the serving producer writes.

        The producer opens its own writer, which retires the one this request borrowed, so
        the rest of the request is bound to a fresh checkout. Its mutation boundary and
        active mutation stay held throughout, so no release or publication runs meanwhile.
        """
        from .preview import preview_store, unbound

        manager, thread = self.manager, threading.get_ident()
        with manager._lock:
            held = manager._store_borrowers.pop(thread, 0)
        # The producer clears ``resume`` when it leaves the store to a pending create, which
        # no fresh checkout can enter; the rest of the request then runs unbound.
        lend = SimpleNamespace(resume=True)

        def resume():
            with manager._lock:
                manager._store_borrowers[thread] = manager._store_borrowers.get(thread, 0) + held
            self._scope.enter_context(preview_store(self.root, self.runtime) if lend.resume else unbound())

        try:
            yield lend
        except BaseException:
            with suppress(Exception):
                resume()
            raise
        resume()

    def _create_tick(self):
        """Finish a create whose replica publication did not complete; returns the seconds until the next tick.

        Until it finishes, the create owns the store and every store request refuses as
        busy. It resumes through the create's own recovery and publication, as the
        in-process owner, never waiting for an owner step. A publication still pending
        retries after the idle interval; any other refusal after the retry interval.
        """
        from ..governance import principal
        from . import admission, authority, capability

        if self.runtime is None or self.session is None or not capability.records_summary_enabled(self.root):
            return IMPORT_IDLE_SECONDS
        try:
            # The resume migrates a store an earlier release left with this intent; until then only its head reads.
            with connection.head_reader(self.runtime.path, self.runtime.reader_pool()) as reader:
                intent = authority.pending_create(reader)
            if intent is None:
                self.pending_create = None
                return IMPORT_IDLE_SECONDS
            if intent["kind"] != authority.CREATE:
                # Only the owner's `exomem collections migrate --apply` finishes a migration: it
                # first rechecks each collection's files against the basis it imported. Only the
                # owner's `exomem collections export` finishes an export, with the service stopped.
                return IMPORT_IDLE_SECONDS
            with principal.library_scope():
                result = admission.resume_local(self.session, self.manager, fence_client=self.session.fence_client)
        except Exception:  # noqa: BLE001 - the create stays pending and is retried
            logger.warning("a pending collection create could not finish; retrying later", exc_info=True)
            return IMPORT_RETRY_SECONDS
        if result["status"] == "marker_admitted":
            self.pending_create = None
            return 0.0
        logger.warning("a pending collection create is still %s: %s", result["status"], result.get("reason"))
        return IMPORT_IDLE_SECONDS if result["status"] == "pending" else IMPORT_RETRY_SECONDS

    def _import_tick(self):
        """Advance running import jobs by one batch; returns the seconds until the next tick."""
        from . import capability, importer
        from .preview import preview_store

        if self.runtime is None or not capability.records_summary_enabled(self.root):
            return IMPORT_IDLE_SECONDS
        try:
            # A plain read first: an idle store takes no lease and no checkout.
            with connection.store_reader(self.runtime.path, self.runtime.reader_pool()) as reader:
                if reader.execute("SELECT 1 FROM import_jobs WHERE state='running' LIMIT 1").fetchone() is None:
                    return IMPORT_IDLE_SECONDS
            with preview_store(self.root, self.runtime):
                done = importer.run_jobs(self.root, max_batches=1, deadline=time.monotonic()
                                         + self.manager._mutation_timeout_seconds / 2)
        except Exception:  # noqa: BLE001 - a refused tick leaves its jobs for a later one
            logger.warning("import jobs could not advance; retrying later", exc_info=True)
            return IMPORT_RETRY_SECONDS
        return 0.0 if done["batches"] else IMPORT_RETRY_SECONDS

    def _backfill_tick(self):
        """Advance one building projection or rollup by one batch; returns the seconds until the next tick.

        Like imports, it runs only while this release enables records-summary-v1, so slice
        rollback stops it. A plain read finds the work, so an idle store takes no lease and
        no checkout. Each tick runs one collection's projection and rollup batch in one
        writer transaction under the mutation boundary, as the in-process owner, taking the
        building collections in turn; a waiting request is taken before the next batch. A
        collection whose batch is refused waits the retry interval; the others keep advancing.
        """
        from ..governance import principal
        from . import capability, index_migrations
        from .preview import _mutate, preview_store

        if self.runtime is None or not capability.records_summary_enabled(self.root):
            return IMPORT_IDLE_SECONDS
        try:
            with connection.store_reader(self.runtime.path, self.runtime.reader_pool()) as reader:
                due = index_migrations.backfill_due(reader)
        except Exception:  # noqa: BLE001 - an unreadable store is retried later
            logger.warning("derived backfill could not read its work; retrying later", exc_info=True)
            return IMPORT_RETRY_SECONDS
        now = time.monotonic()
        self._backfill_refused = {cid: at for cid, at in self._backfill_refused.items()
                                  if at > now and any(cid == row[0] for row in due)}
        due = [row for row in due if row[0] not in self._backfill_refused]
        if not due:
            return min((at - now for at in self._backfill_refused.values()), default=IMPORT_IDLE_SECONDS)
        collection_id, projection, rollup = next((row for row in due if row[0] > self._backfill_last), due[0])
        self._backfill_last = collection_id
        try:
            with principal.library_scope(), preview_store(self.root, self.runtime) as writer:
                try:
                    _mutate(self.root, writer.backfill_derived, collection_id, limit=BACKFILL_BATCH_ROWS,
                            projection=projection, rollup=rollup)
                except Exception:  # noqa: BLE001 - one refused collection leaves the others to advance
                    logger.warning("derived backfill of %s could not advance; retrying later",
                                   collection_id, exc_info=True)
                    self._backfill_refused[collection_id] = time.monotonic() + IMPORT_RETRY_SECONDS
        except Exception:  # noqa: BLE001 - a store that cannot be checked out is retried later
            logger.warning("derived backfill could not check out the store; retrying later", exc_info=True)
            return IMPORT_RETRY_SECONDS
        return 0.0

    def _derived_tick(self):
        """Reconcile a new writer handle's derived file and advance a rebuild one bounded step.

        A restart or a takeover opens a new writer handle, whose first derived tick reconciles
        each derived collection (``derived_rows``). After that, a plain read of the derived file
        finds the rebuilds still running, so an idle store takes no checkout. A rebuild waiting
        on a missing source backs off from ``DERIVED_RETRY_SECONDS`` to ``IMPORT_RETRY_SECONDS``;
        one that finishes lets the import jobs it held continue at once.
        A rebuild is background work: a step ends after
        its batch when another request waits or runs, and the next step waits for it.
        """
        from ..governance import principal
        from . import capability, derived_rows
        from .preview import preview_store

        # The running rebuild's pass continues only from a step that advanced; any other return ends it.
        rebuild, self._derived_pass = self._derived_pass, None
        if self.runtime is None or not capability.records_summary_enabled(self.root):
            return IMPORT_IDLE_SECONDS
        try:
            with connection.store_reader(self.runtime.path, self.runtime.reader_pool()) as reader:
                if reader.execute("SELECT 1 FROM collections WHERE derived=1 LIMIT 1").fetchone() is None:
                    return IMPORT_IDLE_SECONDS
            # An idle release retires the handle without changing the store, so it needs no new reconcile.
            if self._derived_handle is not None \
                    and (self._derived_handle is self.runtime._handle or self._idle_released()) \
                    and not derived_rows.rebuilds_pending(self.runtime.path):
                return IMPORT_IDLE_SECONDS
            if rebuild is not None:
                # A rebuild is one bulk pass across its steps: before the next one it waits while
                # another request runs (``foreground_priority``), holding no boundary and no store
                # checkout, so a handoff or a publication never waits on it. A store request ends
                # the wait (``call``), and the step then ends after one batch (``_aside``).
                with foreground_priority.bulk(budget=rebuild):
                    foreground_priority.yield_to_foreground()
            with principal.library_scope(), preview_store(self.root, self.runtime) as writer:
                outcome = derived_rows.step(self.root, writer, aside=self._aside)
                self._derived_handle = writer.handle
        except Exception:  # noqa: BLE001 - a refused step leaves the rebuild for a later tick
            logger.warning("derived collection rows could not advance; retrying later", exc_info=True)
            return IMPORT_RETRY_SECONDS
        if outcome == "advanced":
            self._derived_pass = rebuild or foreground_priority.PassBudget()
        if outcome == "done":
            self._ticks["import"] = 0.0
        if outcome == "stopped":
            # Preserve a quick transient retry without repeatedly taking the boundary for persistent missing sources.
            retry = self._derived_retry
            self._derived_retry = min(retry * 2, IMPORT_RETRY_SECONDS)
            return retry
        self._derived_retry = DERIVED_RETRY_SECONDS
        return {"advanced": 0.0, "done": 0.0,
                "mismatched": IMPORT_RETRY_SECONDS}.get(outcome, IMPORT_IDLE_SECONDS)

    def _aside(self):
        """Whether background work ends after its current batch or window, which only frees the store sooner.

        A derived rebuild step and a views tick's run of edit-back windows both ask it. It is
        true when a store request is queued, a lease handoff drains the store's borrowers, a writer
        waits for the boundary, or another request runs. The
        waiter count is checked on its own: a request that awaits bulk work releases every pass,
        so ``foreground_elsewhere`` is then false, however many writers wait.
        """
        return (bool(self._requests) or self.manager._store_handoff
                or self.manager._mutation_coordinator_for(self.root).waiter_count() > 0
                or foreground_priority.foreground_elsewhere())

    def _write(self, method, *args, **kwargs):
        """Run one of a views tick's store writes on live placement and policy, not the tick's read scope.

        The same rule as ``_serve``: a write decides on the policy and placement it finds after
        waiting for the mutation boundary, never on the ones the tick read before the wait.
        """
        from ..governance import policy
        from .preview import _mutate

        with state_paths.outside_resolution_scope(), policy.outside_request_policy_snapshot():
            return _mutate(self.root, method, *args, **kwargs)

    def _views_tick(self):
        """Apply settled view edits, then publish pending views; returns the seconds until the next tick,
        negative for a tick that yielded and stays overdue.

        It runs while the release enables ``collection-views-v1`` or ``records-summary-v1``,
        unless the vault's rollback disabled it. Under ``collection-views-v1``, edit-back
        (``edit_back``) reads every item, manifest and log view at its first tick and every 60 s
        after, outside the write transaction, and turns each settled edit into a governed
        update or a held correction. Then a committed change's pending views, such as
        summary pages and log views, publish a window at a time through the store's reconcile path, as
        the in-process owner. A window holds at most
        ``VIEW_BATCH_PATHS`` paths, so one collection's summary pages at their bounds. A
        view installed by one pass is confirmed current by the next, so two passes follow
        each maintenance wake; views still pending then, which cannot publish yet, wait for
        the next wake or the retry interval. Import status polling preserves these passes
        and retry deadlines. Watcher notifications force exact-path reads. Every edit-back or
        census turn uses one bounded window. While no request waits (``_aside``), one checkout
        runs further windows for up to ``VIEW_SLICE_SECONDS``, so a census pays its checkout,
        policy and release checks once per slice, not once per window.

        The tick reads under one view of path placement and governance policy, opened per tick
        and never kept across two. Its store writes decide live (``_write``).

        After this process released the lease on idle, a tick takes the lease again only for work
        it proved without it (``_view_work_needs_lease``); the store thread's own ticks must not
        undo the release. Otherwise it does nothing. A request or a watcher hint takes it as before.

        A tick that starts after a quiet gap (``_tick_may_start``) works as above, but returns
        before its first publication path when a request waits. The pass then stays owed and the
        tick keeps its age, so the starvation bound still counts from when it fell due. A starved
        tick (``_starved``) runs a single bounded window and publishes a single path, even beside
        a waiting request, so each pass still moves under continuous demand.
        """
        from ..governance import policy

        with state_paths.resolution_scope(), policy.request_policy_snapshot():
            return self._views_work()

    def _views_work(self):
        from ..governance import principal
        from . import capability, chain, edit_back
        from .preview import preview_store

        # A starved tick runs one bounded turn whatever else is going on; any other yields at its next seam.
        starved = self._starved("views", time.monotonic())
        owed, self._views_owed = self._views_owed, False  # this tick runs the owed pass, unless it yields
        if self.runtime is None or not capability.views_published(self.root):
            self._view_retry_at = time.monotonic() + IMPORT_IDLE_SECONDS
            return IMPORT_IDLE_SECONDS
        views = capability.collection_views_enabled(self.root)
        self._view_retry_at = 0.0 if views else time.monotonic() + IMPORT_IDLE_SECONDS
        if self._idle_released() and not self._view_work_needs_lease(views, owed):
            return IMPORT_IDLE_SECONDS
        if views and (self._edit_back is None or self._edit_handle is not self.runtime._handle):
            self._edit_back = edit_back.EditBack()
        with self._condition:
            changes = [change for change, checked in self._view_changes.items() if not checked][:VIEW_BATCH_PATHS]
            for change in changes:
                self._view_changes[change] = True
            dirty = self._view_census_needed
            self._view_census_needed = False
        paths = {path for path, destination, directory in changes if destination is None and not directory}
        # Without edit-back, the publication pass alone sets the next tick, as before it existed.
        wait = self._edit_back.wait(time.monotonic()) if views else float("inf")
        if views and (changes or dirty):
            wait = 0.0
        if views and wait <= 0:
            try:
                with principal.library_scope(), preview_store(self.root, self.runtime) as writer:
                    owned = self._edit_back.notify(writer, paths)
                    self._edit_back.hints(writer, [change for change in changes
                                                  if change[0] not in owned or change[1] is not None or change[2]], dirty)

                    def mutate(method, *args, **kwargs):
                        before = chain.recorded_head(writer.connection)
                        result = self._write(method, *args, **kwargs)
                        if chain.recorded_head(writer.connection) != before:
                            # A new canonical edit needs its own install and confirmation passes.
                            self._views_after, self._views_passes = None, 0
                        return result

                    started = time.monotonic()
                    wait = self._edit_back.run(writer, now=started, mutate=mutate, limit=VIEW_BATCH_PATHS)
                    self._edit_handle = self.runtime._handle
                    self._sweep_seen.update(self._sweep_pending)  # a leased pass has now run on what woke it
                    self._sweep_pending = {}
                    with self._condition:
                        for change in changes:
                            if self._view_changes.get(change) is True:
                                del self._view_changes[change]
                    # The census holds these hints now, so a later window's failure must not return them.
                    changes, dirty = [], False
                    # Each window still proves ownership and authorizes its own writes; only the
                    # checkout's admission is shared, and a waiting request ends the slice.
                    while (wait <= 0 and not starved and not self._aside() and not self._stopping()
                           and time.monotonic() - started < VIEW_SLICE_SECONDS):
                        wait = self._edit_back.run(writer, now=time.monotonic(), mutate=mutate,
                                                   limit=VIEW_BATCH_PATHS)
            except Exception:  # noqa: BLE001 - an edit that cannot be classified now is read again later
                logger.warning("collection view edits could not be applied; retrying later", exc_info=True)
                self._sweep_pending, self._sweep_seen = {}, {}  # the next sweep finds the edits again
                wait = IMPORT_RETRY_SECONDS
                self._view_retry_at = time.monotonic() + IMPORT_RETRY_SECONDS
                with self._condition:
                    self._view_census_needed |= dirty
                    for change in changes:
                        if change in self._view_changes:
                            self._view_changes[change] = False
        with self._condition:
            if views and self._view_retry_at <= time.monotonic() \
                    and any(not checked for checked in self._view_changes.values()):
                wait = 0.0
        try:
            from . import history

            # Every file-held input of a history release, not the policy alone: no census render repairs the rest.
            inputs = history.release_inputs(self.root)
            with connection.store_reader(self.runtime.path, self.runtime.reader_pool()) as reader:
                queued = self._history_queued(reader, inputs)
            if not queued:
                with principal.library_scope(), preview_store(self.root, self.runtime) as writer:
                    inputs = self._write(writer.invalidate_history_pages)
                    self._history_policy = (*self._store_ids(writer.connection), inputs,
                                            *chain.recorded_head(writer.connection))
            with connection.store_reader(self.runtime.path, self.runtime.reader_pool()) as reader:
                if reader.execute("SELECT 1 FROM projection_state WHERE state='pending' LIMIT 1").fetchone() is None:
                    self._views_after, self._views_passes = None, 0
                    return min(wait, IMPORT_IDLE_SECONDS)
            if not starved and self._aside():
                # Leave the pass where it is and owed, so it stays views work, and keep this tick's age, so
                # a run of yields cannot reset the starvation clock: a starved tick still publishes a path.
                self._views_owed = True
                return self._ticks["views"] - time.monotonic()
            with principal.library_scope(), preview_store(self.root, self.runtime) as writer:
                # Finish one path even under continuous demand; release its boundary before checking for readers.
                for _ in range(VIEW_BATCH_PATHS):
                    result = self._write(writer.reconcile_views, limit=1,
                                         after=self._views_after, pending_only=True,
                                         missing_ready=writer.handle.census(self.root).missing_ready if views else None)
                    self._views_after = result["next_path"]
                    if self._views_after is None or self._aside() or self._stopping():
                        break
        except Exception:  # noqa: BLE001 - views a tick cannot publish stay pending for a later one
            logger.warning("pending collection views could not publish; retrying later", exc_info=True)
            self._views_after = None
            return min(wait, IMPORT_RETRY_SECONDS)
        if self._views_after is None:
            self._views_passes += 1
        return min(wait, 0.0 if self._views_after is not None or self._views_passes < 2 else IMPORT_RETRY_SECONDS)

    def _idle_released(self):
        """Whether this process released the lease on idle and has not taken it again.

        A lost lease or a standby store is not this: its ticks run as before."""
        return self.manager._store_released and self.manager._fencing_token is None

    @staticmethod
    def _store_ids(conn):
        ids = dict(conn.execute("SELECT key,value FROM store_meta WHERE key IN (?,?)",
                                (schema.META_STORE_ID, schema.META_INSTANCE_ID)))
        return ids.get(schema.META_STORE_ID), ids.get(schema.META_INSTANCE_ID)

    def _history_queued(self, reader, inputs):
        """Whether the drain already queued the history pages of this store, instance and release inputs.

        The writer handle is not part of the answer: taking the lease again opens a new one over the
        same store, whose pages are as current as before. A store that moved on from the head the
        drain saw is the same store; one that forked or went back, another instance, or other
        release inputs call for every page again."""
        from . import chain

        if self._history_policy is None:
            return False
        store_id, instance_id, queued_inputs, *head = self._history_policy
        if queued_inputs != inputs or self._store_ids(reader) != (store_id, instance_id):
            return False
        # nosemgrep: ep-word-membership -- chain.head_relation returns these closed relation tokens.
        return chain.head_relation(reader, *head) in ("same", "ancestor")

    def _view_work_needs_lease(self, views, owed):
        """Whether a views tick after an idle release has work that needs the lease, proven without it.

        Watcher hints, a publication pass left owed, a first pass never run, release inputs the
        queued history pages predate, and an edited view file all prove work. Nothing else does:
        the release drained the views, and only a leased writer commits new ones."""
        from . import history

        with self._condition:
            hinted = self._view_census_needed or any(not checked for checked in self._view_changes.values())
        if hinted or owed or self._views_after is not None or self._history_policy is None \
                or (views and self._edit_back is None):
            return True
        try:
            if history.release_inputs(self.root) != self._history_policy[2]:
                return True
        except Exception:  # noqa: BLE001 - inputs that cannot be read go to the leased tick, which reports them
            return True
        return views and self._view_file_changed()

    def _view_file_changed(self):
        """Whether a view file differs from the store's record, by a sweep that takes no lease.

        It stands in for the leased full pass of edit-back and repeats at the same cadence, so an
        owner's edit is absorbed with the watcher off. It yields to any waiting request between pages and
        continues from where it stopped. A change found wakes the tick once, and the tick's leased pass
        then remembers it: a view that pass left changed does not wake the tick again until its file or
        its record changes. A wake that fails before that pass runs forgets it, and the next sweep finds it."""
        from . import edit_back

        started = time.monotonic()
        if self._sweep_after is None:
            if started < self._sweep_due:
                return False
            self._sweep_after = ""
        found = False
        try:
            with connection.store_reader(self.runtime.path, self.runtime.reader_pool()) as reader:
                while self._sweep_after is not None:
                    changed, self._sweep_after = edit_back.changed_views(reader, self.root, after=self._sweep_after)
                    fresh = {path: seen for path, seen in changed.items() if self._sweep_seen.get(path) != seen}
                    if fresh:
                        self._sweep_pending = fresh
                        self._sweep_after, found = None, True
                    elif self._sweep_after is not None and (
                            self._aside() or self._stopping() or time.monotonic() - started >= VIEW_SLICE_SECONDS):
                        return False
        except Exception:  # noqa: BLE001 - a store that cannot be read now proves no work; the next sweep retries
            logger.warning("view files could not be compared with the idle store; retrying later", exc_info=True)
            self._sweep_after = None
        self._sweep_due = started + edit_back.FULL_PASS_SECONDS
        return found

    def _close(self):
        with self._condition:
            self._closed = True
            for request in self._requests:
                request.taken = True
                request.error = connection.busy("the collection store is shutting down")
                request.done.set()
            self._requests.clear()
            self._condition.notify_all()
        with _SERVERS_LOCK:
            if _SERVERS.get(self.root) is self:
                del _SERVERS[self.root]
        if self.runtime is not None:
            self.runtime.retire_idle_handle()
        if self._stack is not None:
            self._stack.close()
