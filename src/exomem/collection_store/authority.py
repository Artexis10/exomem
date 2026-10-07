"""Private mixed-authority marker parsing and pending-create projection eligibility.

This does not admit a service, migrate files, or publish a marker. Unrelated dark
preview collections retain their existing behavior until production routing exists.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import collection_profiles, held_fs, memory_refs, vault
from .. import structured_collections as collections
from .connection import CollectionStoreError

PENDING_CREATE = "pending_collection_create"
#: The semantic profiles a marker entry may record; an entry from before entries carried one has none.
PROFILES = tuple(collection_profiles.PROFILES)
MARKER_REQUIRED = "collection_store_marker_required"


def marker_path(root):
    return Path(root) / vault.kb_dirname() / "_Collections" / "mode.json"


def read_marker(root):
    path = marker_path(root).relative_to(root)
    with held_fs.acquire(root).require() as fs:
        parent = fs.parent(path.parent.as_posix())
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
                or entry.get("authority") != "store" or entry.get("store_id") != sid
                or entry.get("semantic_profile") not in (None, *PROFILES)):
            _invalid()
        portable = collections._portable_path_key(path)
        if cid in ids or portable in paths:
            _invalid()
        ids.add(cid)
        paths.add(portable)
    return marker


def marker_entry(collection_id, manifest_path, store_id, semantic_profile):
    """One marker entry routing a collection to the store, with the profile it was created with.

    The marker is the routing authority, so a sweep that cannot read the store takes the
    collection's profile from here, never from its editable generated view.
    """
    return {"collection_id": collection_id, "manifest_path": manifest_path, "authority": "store",
            "store_id": store_id, "semantic_profile": semantic_profile}


def routes(entry, collection_id, manifest_path, store_id):
    """Whether ``entry`` routes this collection, at this path, to this store.

    An entry written before entries carried their profile routes the same way.
    """
    return (entry["collection_id"], entry["manifest_path"], entry["authority"], entry["store_id"]) == (
        collection_id, manifest_path, "store", store_id)


def routing_marker(writer):
    raw = read_marker(writer.root)
    return None if raw is None else parse_marker(writer.root, raw)


def selected_entry(root, marker, selector):
    if isinstance(selector, collections.CollectionManifest):
        selector = selector.path
    raw = str(selector).strip()
    identity = memory_refs.parse_memory_ref(raw) or memory_refs.normalize_id(raw)
    path = collections._reference_key(Path(root), raw) if identity is None else None
    return next((entry for entry in marker["collections"]
                 if entry["collection_id"] == identity or (
                     path is not None and collections._portable_path_key(entry["manifest_path"])
                     == collections._portable_path_key(path))), None)


def require_selected(conn, marker, entry, *, root):
    sid = conn.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()
    row = conn.execute(
        "SELECT c.manifest_path,m.manifest_text FROM collections c JOIN collection_manifests m "
        "ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version "
        "WHERE c.collection_id=?", (entry["collection_id"],),
    ).fetchone()
    if sid is None or sid[0] != marker["store_id"] or (row is not None and row[0] != entry["manifest_path"]):
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "store differs from authority marker")
    if row is not None and entry.get("semantic_profile") is not None:
        manifest = collections.parse_manifest_bytes(root, row[0], row[1].encode())
        if manifest.semantic_profile != entry["semantic_profile"]:
            # A false refusal costs a marker repair; accepting hides rows from profile sweeps.
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "marker profile differs from canonical manifest")


def required_state_compatibility_ids(root):
    """Read launch requirements from the existing authority marker only."""
    raw = read_marker(root)
    if raw is None:
        return frozenset()
    marker = parse_marker(root, raw)
    return frozenset({marker["collection_store_fence"]["capability"]})


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
    return intent_status(writer.root, writer.connection, intent)


def intent_status(root, conn, intent):
    """Whether a pending create's marker is admitted, still pending, or in conflict; any connection reads it."""
    try:
        target_marker = parse_marker(root, intent["target_marker"])
        sid = conn.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
        if target_marker["store_id"] != sid or not any(
                routes(entry, intent["collection_id"], intent["manifest_path"], sid)
                for entry in target_marker["collections"]):
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
    if intent is not None and projection["collection_id"] == intent["collection_id"]:
        return marker_status(writer, intent) == "marker_admitted"
    return collection_admitted(writer, projection["collection_id"])


def collection_admitted(writer, collection_id):
    try:
        raw = read_marker(writer.root)
        if raw is None:
            return writer.connection.execute(
                "SELECT 1 FROM store_meta WHERE key=?", (MARKER_REQUIRED,)
            ).fetchone() is None
        marker = parse_marker(writer.root, raw)
        sid = writer.connection.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
        row = writer._collection_row(collection_id)
        return marker["store_id"] == sid and any(
            routes(entry, collection_id, row["manifest_path"], sid) for entry in marker["collections"])
    except (held_fs.HeldFsError, OSError, CollectionStoreError):
        return False


def orphan_cleanup_eligible(writer, directory):
    """A directory sweep cannot attribute an unjournaled file to its trigger.

    Pending creation delays only ambiguous orphan cleanup in its view subtree;
    identified neighboring projections can still recover and publish normally.
    """
    directory = collections._portable_path_key(directory.as_posix())
    for cid, manifest, source, layout in writer.connection.execute(
        "SELECT collection_id,manifest_path,source_path,layout FROM collections"
    ):
        if projection_eligible(writer, {"collection_id": cid}):
            continue
        source = Path(source)
        protected = (Path(manifest).parent, source.parent if layout == "markdown-log" else source)
        if any(directory == (key := collections._portable_path_key(parent.as_posix()))
               or directory.startswith(key + "/") for parent in protected):
            return False
    return True
