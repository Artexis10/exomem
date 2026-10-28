"""Exact imported source bytes for reverse export; ordinary metadata reads omit them."""

from __future__ import annotations

import base64
import hashlib
import json
from contextlib import closing

# This namespace and record version are a closed storage protocol, not vocabulary.
PREFIX = "legacy-original:"
_END = PREFIX[:-1] + chr(ord(PREFIX[-1]) + 1)


def metadata(conn):
    """Read control metadata without loading the archived source payloads."""
    return dict(conn.execute("SELECT key,value FROM store_meta WHERE key<? UNION ALL "
                             "SELECT key,value FROM store_meta WHERE key>=?", (PREFIX, _END)))


def _key(cid, kind, identity):
    return PREFIX + cid + ":" + json.dumps([kind, identity], separators=(",", ":"))


def checkpoint(conn, cid):
    row = conn.execute("SELECT transition_id,event_hash,receipt_json FROM txns WHERE collection_id=? "
                       "AND operation='legacy_import' AND legacy_event_json IS NULL", (cid,)).fetchone()
    return None if row is None else (*row[:2], json.loads(row[2]))


def put(conn, cid, kind, identity, path, raw, basis):
    value = {"version": 1, "collection_id": cid, "kind": kind, "identity": identity, "path": path,
             "sha256": hashlib.sha256(raw).hexdigest(), "checkpoint_transition": basis[0],
             "checkpoint_hash": basis[1], "bytes": base64.b64encode(raw).decode("ascii")}
    conn.execute("INSERT INTO store_meta(key,value) VALUES(?,?)", (_key(cid, kind, identity),
                 json.dumps(value, separators=(",", ":"))))


def get(conn, cid, kind, identity, *, basis=None):
    row = conn.execute("SELECT value FROM store_meta WHERE key=?", (_key(cid, kind, identity),)).fetchone()
    if row is None:
        return None
    from .connection import CollectionStoreError

    try:
        value = json.loads(row[0])
        basis = basis if basis is not None else checkpoint(conn, cid)
        raw = base64.b64decode(value["bytes"], validate=True)
        if (value["version"] != 1 or basis is None
                or (value["collection_id"], value["kind"], value["identity"]) != (cid, kind, identity)
                or (value["checkpoint_transition"], value["checkpoint_hash"]) != basis[:2]
                or hashlib.sha256(raw).hexdigest() != value["sha256"]):
            raise ValueError("original source proof differs")
        return value["path"], raw
    except (ValueError, TypeError, KeyError) as error:
        # Invalid originals must never replace a person's files during reverse export.
        raise CollectionStoreError("COLLECTION_STORE_LEGACY_ORIGINAL_UNVERIFIED",
                                   "the imported source bytes cannot be verified") from error


def owns_file_receipt(conn, proof, root, path, after_hash):
    """Whether current SQL ownership succeeded these exact imported file bytes."""
    from . import legacy_import, tokens
    from .connection import CollectionStoreError

    owner = proof.owner(root, path)
    if owner is None or after_hash is None:
        return False
    row = conn.execute(
        "SELECT p.kind,p.collection_id,i.item_key,t.transition_id,t.event_hash,t.receipt_json,"
        "m.manifest_text,m.manifest_hash FROM projection_state p "
        "JOIN collections c ON c.collection_id=p.collection_id "
        "JOIN txns t ON t.txn_id=c.created_txn "
        "LEFT JOIN items i ON i.row_id=p.row_id AND i.collection_id=p.collection_id "
        "LEFT JOIN collection_manifests m ON m.collection_id=c.collection_id AND m.manifest_version=1 "
        "WHERE p.path=? AND p.collection_id=? AND t.operation='legacy_import' AND t.legacy_event_json IS NULL",
        (path, owner["collection_id"]),
    ).fetchone()
    if row is None:
        return False
    kind, cid, key, transition, digest, receipt, manifest, manifest_hash = row
    try:
        envelope = json.loads(receipt)
        if (envelope["tag"], envelope["version"], envelope["phase"], envelope["collection_id"],
                envelope["transition_id"], envelope["manifest_version_after"]) != (
                "exomem-legacy-import", 1, "checkpoint", cid, transition, 1
        ) or legacy_import._event_hash(envelope) != digest:
            return False
        # These projection kinds are the closed storage schema, not user vocabulary.
        if kind == "manifest":
            return (path == owner["manifest_path"] and tokens.manifest_hash(manifest) == manifest_hash
                    and hashlib.sha256(manifest.encode("utf-8")).hexdigest() == after_hash)
        if kind not in {"item", "log"}:
            return False
        original = get(conn, cid, kind, key if kind == "item" else "source",
                       basis=(transition, digest))
        if original is None or original[0] != path or hashlib.sha256(original[1]).hexdigest() != after_hash:
            return False
        return kind != "item" or envelope["original_items"][key]["source_sha256"] == after_hash
    except (CollectionStoreError, KeyError, TypeError, ValueError):
        # Missing or damaged migration evidence leaves this exact custody owed.
        return False


def projections(conn, cid):
    """The schema's source projections, keyed by immutable item identity or the log source."""
    return conn.execute("SELECT p.path,p.kind,i.item_key,p.published_sha256 FROM projection_state p "
                        "LEFT JOIN items i ON i.row_id=p.row_id AND i.collection_id=p.collection_id "
                        "WHERE p.collection_id=? AND p.kind IN ('item','log') ORDER BY p.path", (cid,))


def material(conn, cid):
    basis = checkpoint(conn, cid)
    result = []
    for _path, kind, key, _digest in projections(conn, cid):
        original = get(conn, cid, kind, key if kind == "item" else "source", basis=basis)
        if original is None:
            return None
        result.append((kind, key if kind == "item" else "source", *original))
    return tuple(sorted(result))


def export_sources(root, conn, cid):
    """Restore exact sources; an older pristine import can still prove its bytes in place."""
    from .. import record_formats, vault
    from .connection import CollectionStoreError

    basis, targets = checkpoint(conn, cid), {}
    for path, kind, key, published in projections(conn, cid):
        original = get(conn, cid, kind, key if kind == "item" else "source", basis=basis)
        if original is not None:
            targets[path] = original[1]
            continue
        expected = basis[2].get("original_items", {}).get(key, {}).get("source_sha256") if kind == "item" else published
        try:
            raw, guard = vault.read_bounded_guarded_bytes(root, path,
                limit=record_formats._MAX_ITEM_BYTES if kind == "item" else record_formats._MAX_LOG_BYTES,
                expected_hash=expected)
            guard.recheck(root)
        except (vault.PathGuardError, OSError) as error:
            raise CollectionStoreError("COLLECTION_STORE_LEGACY_ORIGINAL_UNVERIFIED",
                                       "restore the original imported source before exporting") from error
        targets[path] = raw
    return targets


def view_basis(conn, projection):
    """An imported v1 pair remains a base only until its first canonical render is confirmed."""
    if projection["kind"] == "log":
        cid = projection["collection_id"]
        archived = conn.execute("SELECT json_extract(value,'$.sha256') FROM store_meta WHERE key=?",
                                (_key(cid, "log", "source"),)).fetchone()
        if archived is None or archived[0] != projection["published_sha256"]:
            return None
        get(conn, cid, "log", "source")
        return archived[0], True
    if projection["published_row_version"] != 1:
        return None
    if projection["kind"] == "manifest":
        row = conn.execute("SELECT m.manifest_hash FROM collections c JOIN txns t ON t.txn_id=c.created_txn "
            "JOIN collection_manifests m ON m.collection_id=c.collection_id AND m.manifest_version=1 "
            "WHERE c.collection_id=? AND c.manifest_version=1 AND t.operation='legacy_import' "
            "AND t.legacy_event_json IS NULL", (projection["collection_id"],)).fetchone()
        return (row[0], True) if row is not None and row[0] == projection["published_sha256"] else None
    if projection["kind"] != "item":
        return None
    row = conn.execute("SELECT i.item_key,t.transition_id,t.event_hash FROM items i JOIN txns t "
        "ON t.txn_id=i.created_txn WHERE i.row_id=? AND i.collection_id=? AND i.row_version=1 "
        "AND t.operation='legacy_import' AND t.legacy_event_json IS NULL",
        (projection["row_id"], projection["collection_id"])).fetchone()
    if row is None:
        return None
    cid, key = projection["collection_id"], row[0]
    archived = conn.execute("SELECT json_extract(value,'$.sha256') FROM store_meta WHERE key=?",
                            (_key(cid, "item", key),)).fetchone()
    if archived is not None:
        if archived[0] != projection["published_sha256"]:
            return None
        get(conn, cid, "item", key, basis=row[1:])
        return archived[0], True
    # Older admitted imports can prove an offline rename before their original path is found.
    expected = checkpoint(conn, cid)[2].get("original_items", {}).get(key, {}).get("source_sha256")
    return (expected, False) if expected is not None and expected == projection["published_sha256"] else None


def current_view_matches(conn, projection, raw):
    """Prove an imported view's bytes still describe its current canonical content."""
    from .connection import CollectionStoreError

    kind = projection["kind"]
    if kind == "log":
        # A whole log's editable base survives writes; its read authority must not.
        current = conn.execute(
            "SELECT created_txn=updated_txn FROM collections WHERE collection_id=?",
            (projection["collection_id"],),
        ).fetchone()
        if current != (1,):
            return False
    elif kind == "item":
        # A revised schema can reinterpret unchanged row bytes; require their original contract.
        current = conn.execute(
            "SELECT manifest_version FROM collections WHERE collection_id=?",
            (projection["collection_id"],),
        ).fetchone()
        if current != (1,):
            return False
    try:
        basis = view_basis(conn, projection)
    except CollectionStoreError:
        return False
    return basis is not None and hashlib.sha256(raw).hexdigest() == basis[0]


def backfill(writer, check):
    """Capture recoverable older imports before publication, never reconstructing original bytes."""
    from .. import record_formats, vault

    conn, root = writer.connection, writer.root
    for (cid,) in conn.execute("SELECT c.collection_id FROM collections c JOIN txns t ON t.txn_id=c.created_txn "
                              "WHERE t.operation='legacy_import' AND t.legacy_event_json IS NULL").fetchall():
        basis = checkpoint(conn, cid)
        for path, kind, key, published in list(projections(conn, cid)):
            identity = key if kind == "item" else "source"
            if conn.execute("SELECT 1 FROM store_meta WHERE key=?", (_key(cid, kind, identity),)).fetchone():
                continue
            expected = basis[2].get("original_items", {}).get(key, {}).get("source_sha256") if kind == "item" else published
            if expected is None or kind == "log" and conn.execute(
                    "SELECT 1 FROM txns WHERE collection_id=? AND operation!='legacy_import' LIMIT 1", (cid,)).fetchone():
                continue
            try:
                raw, guard = vault.read_bounded_guarded_bytes(root, path,
                    limit=record_formats._MAX_ITEM_BYTES if kind == "item" else record_formats._MAX_LOG_BYTES,
                    expected_hash=expected)
                with writer.handle.transaction():
                    check()
                    guard.recheck(root)
                    put(conn, cid, kind, identity, path, raw, basis)
            except (vault.PathGuardError, OSError):
                # Missing or edited bytes stay with edit-back; they are never a new import original.
                continue


def needs_replica_copy(root, conn):
    """A same-head backfill must reach the replica before its only original file is stamped."""
    from . import connection, replica, schema

    def headers(reader):
        return tuple(reader.execute("SELECT key,json_extract(value,'$.sha256'),"
            "json_extract(value,'$.checkpoint_hash') FROM store_meta WHERE key>=? AND key<? ORDER BY key",
            (PREFIX, _END)))

    expected = headers(conn)
    if not expected:
        return False
    path = replica.replica_path(root)
    if not path.exists():
        return True
    reader, found = connection._open_read_only(path, connection.BUSY_TIMEOUT_MS)
    with closing(reader):
        # A replica an earlier schema published is copied at this one; refusing it would stop
        # the takeover that just migrated the live store on every retry.
        return found < schema.SCHEMA_VERSION or headers(reader) != expected
