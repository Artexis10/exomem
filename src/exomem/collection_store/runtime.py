"""Vault-bound lifetime for admitted disposable preview stores.

The production create/adoption, mode, fence and custody producer is not present.
An explicit trusted authority check is required; the preview flag supplies none.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, closing, contextmanager
from functools import wraps
from pathlib import Path

from . import connection, replica, schema


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

    def __init__(self, vault_root: Path, manager, *, authority_check: Callable[[], bool]):
        self.root = Path(vault_root).resolve()
        self.path = connection.store_path(self.root).resolve()
        self.manager = manager
        self._authority_check = authority_check
        self._handle = None
        self._writer_token = None
        self._identity = None
        initial = self.sample_head()
        self._identity = (initial.store_id, initial.instance_id)
        with manager._lock:
            if manager._collection_store is not None:
                raise ValueError("the lease manager already owns a collection store runtime")
            manager._collection_store = self

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

    def _admit(self, token):
        if self.manager.config.enabled:
            self.manager.validate_fencing_token(token)
        if not self._authority_check():
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_LEASE_REQUIRED", "trusted preview admission is unavailable"
            )
        self.sample_head()

    def _authority(self, token, *, progress=False):
        if progress:
            self.manager._renew_collection_store(token, due_only=True)
        self._admit(token)
        return self.manager._mutation_coordinator_for(self.root).current_thread_holds_boundary()

    @contextmanager
    def checkout(self):
        with self.manager._collection_store_checkout():
            lease = self.manager.ensure_writer()
            self._admit(lease.fencing_token)
            if self._handle is None or self._writer_token != lease.fencing_token:
                with self.manager.consistency_guard(self.root, operation="collection_store_open"):
                    with self.manager._lock:
                        if self._handle is not None and self._writer_token != lease.fencing_token:
                            if (self.manager._store_borrowers != {threading.get_ident(): 1}
                                    or self._handle.connection.in_transaction):
                                raise connection.CollectionStoreError(
                                    "COLLECTION_STORE_BUSY", "the stale writer is still borrowed"
                                )
                            # Quiescent close is the only cross-thread handle operation.
                            self._handle.close()
                            self._handle = None
                            self._writer_token = None
                        if self._handle is None:
                            self.sample_head()
                            self._handle = connection.open_writer(
                                self.path, lease_check=lambda: self._authority(lease.fencing_token)
                            )
                            self._writer_token = lease.fencing_token
            with borrowed_writer(self.root, self._handle, self.manager) as writer:
                yield writer

    def flush(self, token, *, deadline, cancelled=None):
        """Called only after admission stops, drainage and boundary acquisition."""
        if not self._authority(token, progress=True):
            raise connection.CollectionStoreError(
                "COLLECTION_STORE_LEASE_REQUIRED", "publication authority is unavailable"
            )
        handle = self._handle
        if handle is not None:
            if handle.connection.in_transaction:
                raise connection.CollectionStoreError(
                    "COLLECTION_STORE_BUSY", "the retired writer still has a transaction"
                )
            self._handle = None
            self._writer_token = None
            handle.close()
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
