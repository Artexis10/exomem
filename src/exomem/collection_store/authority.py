"""Private mixed-authority marker parsing and pending-create projection eligibility.

This does not admit a service, migrate files, or publish a marker. Unrelated dark
preview collections retain their existing behavior until production routing exists.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import held_fs, memory_refs, vault
from .. import structured_collections as collections
from .connection import CollectionStoreError

PENDING_CREATE = "pending_collection_create"


def marker_path(root):
    return Path(root) / vault.kb_dirname() / "_Collections" / "mode.json"


def read_marker(root):
    path = marker_path(root).relative_to(root)
    with held_fs.acquire(root).require() as fs:
        parent = fs.parent(str(path.parent))
        if parent.error is not None and parent.error.code == "MISSING":
            return None
        with parent.require() as directory:
            opened = fs.file(directory, path.name)
            if opened.error is not None and opened.error.code == "MISSING":
                return None
            with opened.require() as file:
                return fs.read(file).require()


def _invalid():
    raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "invalid mixed-authority marker")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            _invalid()
        result[key] = value
    return result


def parse_marker(root, raw):
    """Validate only the version-one, file-default marker used by this leaf."""
    try:
        marker = json.loads(raw, object_pairs_hook=_object)
    except (ValueError, UnicodeError, TypeError) as error:
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "unreadable mode marker") from error
    if not isinstance(marker, dict):
        _invalid()
    sid = marker.get("store_id")
    epoch = marker.get("authority_epoch")
    fence = marker.get("collection_store_fence")
    if (type(marker.get("version")) is not int or marker["version"] != 1
            or marker.get("mode") != "store" or marker.get("default_authority") != "file"
            or not isinstance(sid, str) or memory_refs.normalize_id(sid) != sid
            or type(epoch) is not int or epoch < 1
            or not isinstance(fence, dict) or fence.get("capability") != "collections-store-v1"
            or type(fence.get("generation")) is not int or fence["generation"] < 1
            or not isinstance(marker.get("collections"), list) or not marker["collections"]):
        _invalid()
    ids, paths = set(), set()
    for entry in marker["collections"]:
        if not isinstance(entry, dict):
            _invalid()
        cid, path = entry.get("collection_id"), entry.get("manifest_path")
        if (not isinstance(cid, str) or memory_refs.normalize_id(cid) != cid
                or not isinstance(path, str) or collections._reference_key(Path(root), path) != path
                or Path(path).name != "_collection.md"
                or entry.get("authority") != "store" or entry.get("store_id") != sid):
            _invalid()
        portable = collections._portable_path_key(path)
        if cid in ids or portable in paths:
            _invalid()
        ids.add(cid)
        paths.add(portable)
    return marker


def pending_create(conn):
    row = conn.execute("SELECT value FROM store_meta WHERE key=?", (PENDING_CREATE,)).fetchone()
    if row is None:
        return None
    try:
        intent = json.loads(row[0])
        if (type(intent["version"]) is not int or intent["version"] != 1
                or not all(isinstance(intent[name], str) and intent[name] for name in
                           ("request_id", "request_hash", "collection_id", "manifest_path",
                            "source_path", "target_marker"))
                or type(intent["txn_id"]) is not int or intent["txn_id"] < 1
                or not (intent["expected_marker"] is None or isinstance(intent["expected_marker"], str))):
            _invalid()
        return intent
    except (KeyError, TypeError, ValueError) as error:
        raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "invalid pending create intent") from error


def marker_status(writer, intent):
    try:
        root = writer.root
        target_marker = parse_marker(root, intent["target_marker"])
        sid = writer.connection.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
        entry = {"collection_id": intent["collection_id"], "manifest_path": intent["manifest_path"],
                 "authority": "store", "store_id": sid}
        if target_marker["store_id"] != sid or entry not in target_marker["collections"]:
            return "conflict"
        raw = read_marker(root)
        target = intent["target_marker"].encode()
        expected = intent["expected_marker"]
        expected = None if expected is None else expected.encode()
        if raw is not None:
            parse_marker(root, raw)
        if raw == target:
            return "marker_admitted"
        if raw == expected:
            return "pending"
    except (held_fs.HeldFsError, OSError, CollectionStoreError):
        pass
    return "conflict"


def projection_eligible(writer, projection):
    intent = pending_create(writer.connection)
    if intent is None or projection["collection_id"] != intent["collection_id"]:
        return True
    return marker_status(writer, intent) == "marker_admitted"


def orphan_cleanup_eligible(writer, directory):
    """A directory sweep cannot attribute an unjournaled file to its trigger.

    Pending creation delays only ambiguous orphan cleanup in its view subtree;
    identified neighboring projections can still recover and publish normally.
    """
    intent = pending_create(writer.connection)
    if intent is None or marker_status(writer, intent) == "marker_admitted":
        return True
    source = Path(intent["source_path"])
    if writer._collection_row(intent["collection_id"])["layout"] == "markdown-log":
        source = source.parent
    directory = collections._portable_path_key(directory.as_posix())
    protected = (Path(intent["manifest_path"]).parent, source)
    return not any(directory == (key := collections._portable_path_key(parent.as_posix()))
                   or directory.startswith(key + "/") for parent in protected)
