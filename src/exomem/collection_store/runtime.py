"""Vault-bound lifetime for admitted collection stores.

Admission comes only from a trusted producer session (``admission``) that holds
custody, fence and takeover authority; the preview flag supplies none. Release
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

from . import connection, replica, schema

logger = logging.getLogger(__name__)

#: A9/N4: steady writes publish the replica at most once per interval, outside the write path.
PUBLISH_INTERVAL_SECONDS = 60.0
#: A burst of commits shares one publication once writes pause this long.
PUBLISH_SETTLE_SECONDS = 1.0
#: The `_Collections/` watch (A4): stat the replica every tick; hash it only when that changes.
WATCH_SECONDS = 1.0
_BUSY_RETRY_SECONDS = 0.05
# Producer-bound runtimes by vault: the export flush and the in-service owner route find them.
_SERVING = weakref.WeakValueDictionary()


class _Publisher:
    """Coalesced replica publication and the `_Collections/` watch for one admitted store.

    Publication copies a pinned snapshot outside the vault-wide mutation boundary, so
    writes, file-collection writes included, proceed during the copy. Only its two
    brief steps hold the boundary, each taken at an idle instant: recording the intent,
    and verifying and swapping the copy; a write arriving during one waits for it. The watch
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
                        # Read while still a borrower: the handle cannot be retired yet, unless
                        # this borrower lent the store to its producer (``StoreServer.lent``).
                        changed = not handle._closed and handle.connection.total_changes != before
            finally:
                if changed and self._publisher is not None:
                    self._publisher.note_commit()

    def _publication_writer(self, token):
        """This store's writer for a publication step; the caller holds the boundary."""
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
        return connection.open_writer(self.path, lease_check=lambda: self._authority(token, progress=True))

    def publish(self, token, *, deadline):
        """Coalesced publication (A9): copy outside the vault-wide boundary, swap under it.

        Each boundary step is brief and taken at an idle instant. The swap step first
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
                with self._publication_writer(token) as writer:
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

    def flush(self, token, *, deadline, cancelled=None):
        """Called only after admission stops, drainage and boundary acquisition."""
        with self._publication_writer(token) as writer:
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


# --- the service's store thread (S1) ----------------------------------------------------

#: The facades whose collection selectors the vault's marker can route to the store.
_STORE_COMMANDS = frozenset({"record_memory", "plan_memory"})
#: How long the idle store thread waits for a request before it looks for import jobs again.
IMPORT_IDLE_SECONDS = 1.0
#: How long it leaves running jobs that advanced nothing (blocked or refused) alone.
IMPORT_RETRY_SECONDS = 30.0
#: Rows per derived backfill batch; each tick runs one batch for one building collection.
BACKFILL_BATCH_ROWS = 128
#: Paths per pending-view publication window: one collection's summary pages at their bounds.
VIEW_BATCH_PATHS = 16
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
    from . import authority

    root = Path(vault_root).resolve()
    with _SERVERS_LOCK:
        _SERVED[root] = manager
    if authority.read_marker(root) is not None:
        _server(root)


def route(command, vault_root, arguments):
    """The store thread a request must run on, or None when file collections answer it.

    A selector the vault's marker routes to the store, a NEW summary collection's create
    and the Records inventory run on the store thread of the service serving this vault;
    a caller that bound its own writer keeps it. Without that service a routed selector
    refuses (C never falls back to its views), a summary create on a vault with no store
    refuses as the owner's enrolment step, and the inventory names C as unreadable.
    """
    from ..cli_ops import OpError
    from . import admission, authority, capability
    from .preview import bound_writer

    root = Path(vault_root).resolve()
    if command not in _STORE_COMMANDS or bound_writer(root) is not None:
        return None
    selector = arguments.get("collection", arguments.get("manifest_path"))
    raw = authority.read_marker(root)
    if command == "record_memory" and arguments.get("action") == "create" and summary_create(root, arguments):
        if raw is None:
            capability.require_records_summary(root)
            raise admission.enrollment_required()
    elif command == "record_memory" and arguments.get("action") == "inspect" and selector is None:
        server = None if raw is None else _server(root)
        return server if server is not None and server.runtime is not None else None
    elif selector is None or raw is None or (authority.selected_entry(
            root, authority.parse_marker(root, raw), selector) is None and not _creating(root, selector)):
        return None
    server = _server(root)
    if server is None:
        raise OpError(
            "COLLECTION_STORE_UNAVAILABLE",
            "this collection lives in the collection store, which only the running Exomem service serves",
            "Send the request to the service that serves this vault.",
        )
    return server


def _durable_create(root):
    """The marker entry of a create left pending on disk, or None when there is none or it cannot be read.

    File collections route through here too, so a store that cannot be read must leave
    them on their file route rather than refuse them.
    """
    from . import authority

    try:
        if not connection.store_path(root).exists():
            return None
        with closing(connection.open_reader(connection.store_path(root))) as reader:
            intent = authority.pending_create(reader)
    except (connection.CollectionStoreError, sqlite3.Error, OSError):
        return None
    return None if intent is None else {"collection_id": intent["collection_id"],
                                        "manifest_path": intent["manifest_path"]}


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
    return pending is not None and authority.selected_entry(root, {"collections": [pending]}, selector) is not None


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
    if not session.fence_client.collection_store_fence().enrolled:
        # Served, so this vault's store was enrolled: a coordinator that no longer knows it was replaced.
        raise admission.adoption_required()
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
            server.pending_create = {"collection_id": result["collection_id"], "manifest_path":
                                     collections._reference_key(root, arguments["manifest_path"])}
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


class _Call:
    """One request handed to the store thread, run in a copy of the caller's context."""

    def __init__(self, work):
        import contextvars

        self.context = contextvars.copy_context()
        self.work = work
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
    runs one tick, the one that has waited longest. A request it cannot take within the
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
        self._ticks = {"create": 0.0, "import": 0.0, "backfill": 0.0, "views": 0.0}
        self._views_after = None  # where the current pass over pending views continues
        self._views_passes = 0  # completed passes since the last request
        self._backfill_refused = {}  # collection id -> when its refused backfill is retried
        self._backfill_last = ""  # the collection the last batch advanced; the next one follows it
        self.pending_create = None  # the marker entry a served create left pending, until it finishes
        self._requests = deque()
        self._closed = False
        self._condition = threading.Condition()
        self.thread = threading.Thread(target=self._run, name="exomem-collection-store", daemon=True)
        self.thread.start()

    def call(self, work):
        """Run ``work`` on the store thread and return its result or raise its error."""
        request = _Call(work)
        with self._condition:
            if not self._closed:
                self._requests.append(request)
                self._condition.notify_all()
            deadline = time.monotonic() + self.manager._mutation_timeout_seconds
            while not request.taken:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._closed:
                    if request in self._requests:
                        self._requests.remove(request)
                    raise connection.busy(
                        "the collection store did not take this request within the mutation timeout")
                self._condition.wait(remaining)
        request.done.wait()
        if request.error is not None:
            raise request.error
        return request.result

    def _stopping(self):
        return _closed(self.manager)

    def _run(self):
        try:
            while not self._stopping():
                now = time.monotonic()
                if self.runtime is None and now >= self._retry_at:
                    self._open()
                due = min(self._ticks.values())
                request = self._take(max(0.0, min(due, now + IMPORT_IDLE_SECONDS) - now))
                if request is not None:
                    self._serve(request)
                    # The request may have started a job, revised a declaration or left views pending.
                    self._ticks = dict.fromkeys(self._ticks, 0.0)
                    self._views_after, self._views_passes = None, 0
                elif time.monotonic() >= due:
                    # One tick per turn, so a waiting request is taken between any two.
                    name = min(self._ticks, key=self._ticks.get)
                    self._ticks[name] = time.monotonic() + getattr(self, f"_{name}_tick")()
        finally:
            self._close()

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

    def _open(self):
        from ..cli_ops import OpError
        from . import admission

        stack = ExitStack()
        try:
            session = stack.enter_context(admission.production_session(self.root))
            # The writer credential reads the coordinator's fence; moving it is the operator's.
            fence_client = self.manager.client
            if fence_client is None:
                raise connection.CollectionStoreError(
                    "COLLECTION_STORE_LEASE_REQUIRED", "serving the collection store needs the configured writer lease")
            if not fence_client.collection_store_fence().enrolled:
                # A copied vault, or a replaced coordinator, that never enrolled this store: the
                # fence cut is the owner's offline step, which the writer credential cannot make.
                raise admission.adoption_required()
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
        from .preview import preview_store

        def bound():
            with ExitStack() as scope:
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
            with closing(connection.open_reader(self.runtime.path)) as reader:
                if authority.pending_create(reader) is None:
                    self.pending_create = None
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
            with closing(connection.open_reader(self.runtime.path)) as reader:
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
            with closing(connection.open_reader(self.runtime.path)) as reader:
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

    def _views_tick(self):
        """Publish pending views, such as summary pages, a window at a time; returns the seconds until the next tick.

        A committed change leaves summary pages pending; they publish here, after its
        acknowledgement, through the store's reconcile path, as the in-process owner. A
        window holds at most ``VIEW_BATCH_PATHS`` paths, so one collection's summary pages
        at their bounds. A view installed by one pass is confirmed current by the next, so
        two passes follow each request; views still pending then, which cannot publish yet,
        wait for the next request or the retry interval.
        """
        from ..governance import principal
        from . import capability
        from .preview import _mutate, preview_store

        if self.runtime is None or not capability.records_summary_enabled(self.root):
            return IMPORT_IDLE_SECONDS
        try:
            with closing(connection.open_reader(self.runtime.path)) as reader:
                if reader.execute("SELECT 1 FROM projection_state WHERE state='pending' LIMIT 1").fetchone() is None:
                    self._views_after, self._views_passes = None, 0
                    return IMPORT_IDLE_SECONDS
            with principal.library_scope(), preview_store(self.root, self.runtime) as writer:
                result = _mutate(self.root, writer.reconcile_views, limit=VIEW_BATCH_PATHS,
                                 after=self._views_after, pending_only=True)
        except Exception:  # noqa: BLE001 - views a tick cannot publish stay pending for a later one
            logger.warning("pending collection views could not publish; retrying later", exc_info=True)
            self._views_after = None
            return IMPORT_RETRY_SECONDS
        self._views_after = result["next_path"]
        if self._views_after is None:
            self._views_passes += 1
        return 0.0 if self._views_after is not None or self._views_passes < 2 else IMPORT_RETRY_SECONDS

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
