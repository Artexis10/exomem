"""Private preparation/recovery leaf; fenced marker publication is a future caller.

One intent references the immutable create receipt. It survives cutover in shared
snapshots without requiring a new business transaction or replica publication.
"""

from __future__ import annotations

import json
import sqlite3

from .. import structured_collections as collections
from ..cli_ops import OpError
from . import authority, chain
from .connection import CollectionStoreError


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
    with writer.read_snapshot():
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
