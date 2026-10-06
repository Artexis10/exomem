"""Vault-bound lifetime for admitted collection stores.

Admission comes only from a trusted producer session (``admission``) that holds
custody, fence and takeover authority; the preview flag supplies none. Public
routes and launcher support remain closed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
import weakref
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, closing, contextmanager
from functools import wraps
from pathlib import Path

from . import connection, replica, schema

logger = logging.getLogger(__name__)

#: A9/N4: steady writes publish the replica at most once per interval, off the acknowledgement.
PUBLISH_INTERVAL_SECONDS = 60.0
#: A burst of commits shares one publication once writes pause this long.
PUBLISH_SETTLE_SECONDS = 1.0
#: The `_Collections/` watch (A4): stat the replica every tick; hash it only when that changes.
WATCH_SECONDS = 1.0
_BUSY_RETRY_SECONDS = 0.05
# Producer-bound runtimes by vault: the export flush and the in-service owner route find them.
_SERVING = weakref.WeakValueDictionary()


class _Publisher:
    """Coalesced off-ack replica publication and the `_Collections/` watch for one admitted store.

    Publication starts only at an idle instant, so it never delays an acknowledgement
    already in progress. It then holds the vault-wide mutation boundary for its whole
    duration: a write that arrives meanwhile, a file-collection write included, waits
    for it and refuses MUTATION_BUSY when it outlasts the mutation timeout. The watch
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
        meta = dict(reader.execute("SELECT key,value FROM store_meta"))
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

    def record_admission(self, token):
        self._admitted_token = token
        self._takeover = None
        self._handed_off = False
        if self._publisher is None:
            self._publisher = _Publisher(self)

    def _meta(self, *keys):
        with closing(connection.open_reader(self.path)) as reader:
            found = dict(reader.execute(
                f"SELECT key,value FROM store_meta WHERE key IN ({','.join('?' * len(keys))})", keys))
        return [found.get(key) for key in keys]

    def replica_current(self):
        """Whether the replica already carries the live committed head (a spurious commit note)."""
        head = self.sample_head()
        (published,) = self._meta(schema.META_PUBLISHED_REPLICA_HEAD)
        return published is not None and (
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
        with closing(connection.open_reader(self.path)) as reader:
            return authority.pending_create(reader) is None

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
            # Quiescent close is the only cross-thread handle operation.
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
        with closing(connection.open_reader(self.path)) as reader:
            reader.execute("BEGIN")
            metadata = dict(reader.execute("SELECT key,value FROM store_meta"))
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
                raise connection.CollectionStoreError(
                    "COLLECTION_STORE_IDENTITY_CHANGED", "the enrolled store identity changed"
                )
            return head

    def _admit(self, token, *, reads=False):
        if self.manager.config.enabled:
            self.manager.validate_fencing_token(token)
        if not self.reporting_ready(token) and not (
            reads and self._takeover is not None and self.path.exists()
        ):
            raise self._refusal()
        self.sample_head()

    def _authority(self, token, *, progress=False, reads=False):
        if progress:
            self.manager._renew_collection_store(token, due_only=True)
        self._admit(token, reads=reads)
        return self.manager._mutation_coordinator_for(self.root).current_thread_holds_boundary()

    @contextmanager
    def checkout(self):
        if self._bootstrap and not self.reporting_ready(self.manager._fencing_token):
            if self._resolver is None:
                raise connection.CollectionStoreError(
                    "COLLECTION_STORE_LEASE_REQUIRED", "isolated recovery has not admitted this token"
                )
            # Takeover/adoption, rechecked every 10 s or on replica change; an unresolved
            # takeover leaves reads of the local copy, and never closes a borrowed handle.
            self._resolver()
        with self.manager._collection_store_checkout():
            lease = self.manager.ensure_writer()
            self._admit(lease.fencing_token, reads=True)
            if self._handle is None or self._writer_token != lease.fencing_token:
                with self.manager.consistency_guard(self.root, operation="collection_store_open"):
                    with self.manager._lock:
                        if self._handle is not None and self._writer_token != lease.fencing_token:
                            if (self.manager._store_borrowers != {threading.get_ident(): 1}
                                    or not self._retire_handle()):
                                raise connection.CollectionStoreError(
                                    "COLLECTION_STORE_BUSY", "the stale writer is still borrowed"
                                )
                        if self._handle is None:
                            self.sample_head()
                            # Opening may serve reads of an unadmitted copy; transactions never.
                            opening = [True]
                            self._handle = connection.open_writer(
                                self.path, lease_check=lambda: self._authority(
                                    lease.fencing_token, reads=opening[0])
                            )
                            opening[0] = False
                            self._writer_token = lease.fencing_token
            handle, changed = self._handle, False
            before = handle.connection.total_changes
            try:
                with borrowed_writer(self.root, handle, self.manager) as writer:
                    try:
                        yield writer
                    finally:
                        # Read while still a borrower: the handle cannot be retired yet.
                        changed = handle.connection.total_changes != before
            finally:
                if changed and self._publisher is not None:
                    self._publisher.note_commit()

    def flush(self, token, *, deadline, cancelled=None):
        """Called only after admission stops, drainage and boundary acquisition."""
        if not self._authority(token, progress=True):
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_LEASE_REQUIRED", "publication authority is unavailable"
            )
        if self._publication_custody is not None and not self._publication_custody(token):
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_CUSTODY_UNVERIFIED", "single-host custody no longer verifies; nothing was published"
            )
        if not self._retire_handle():
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_BUSY", "the retired writer still has a transaction"
            )
        with connection.open_writer(
            self.path, lease_check=lambda: self._authority(token, progress=True)
        ) as writer:
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
            return head
