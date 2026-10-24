"""Private mixed-authority marker parsing and pending-create projection eligibility.

This does not admit a service, migrate files, or publish a marker. Unrelated dark
preview collections retain their existing behavior until production routing exists.
"""

from __future__ import annotations

import copy
import functools
import json
from pathlib import Path

from .. import collection_profiles, held_fs, memory_refs, vault
from .. import structured_collections as collections
from .connection import CollectionStoreError

PENDING_CREATE = "pending_collection_create"
#: The closed kinds of that one pending intent: admission implements a create and a migration,
#: and ``export`` the reverse export back to files.
CREATE, MIGRATE, EXPORT = "create", "migrate", "export"
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
    """Validate the file-default marker and its complete version-two ownership namespace.

    Recall checks ownership for every hit and due entry, so validation runs once per
    marker spelling. Every caller still gets its own copy to change.
    """
    if not isinstance(raw, (str, bytes)):
        # Anything else, a missing marker included, is refused exactly as before, uncached.
        return _parsed_marker.__wrapped__(str(root), vault.kb_dirname(), raw)
    return copy.deepcopy(_parsed_marker(str(root), vault.kb_dirname(), raw))


# Validation reads only the raw bytes, the root and the kb folder name, so those are the key.
# A vault has one current marker; a few slots also cover a pending target and its predecessor.
@functools.lru_cache(maxsize=8)
def _parsed_marker(root, _kb_dirname, raw):
    try:
        marker = json.loads(raw, object_pairs_hook=_object)
    except (ValueError, UnicodeError, TypeError) as error:
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "unreadable mode marker") from error
    if not isinstance(marker, dict):
        _invalid()
    sid = marker.get("store_id")
    epoch = marker.get("authority_epoch")
    fence = marker.get("collection_store_fence")
    if (type(marker.get("version")) is not int or marker["version"] not in (1, 2)
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
        if marker["version"] == 2:
            # The store schema fixes these layouts; dataset collections retain file authority.
            source = entry.get("source_path")
            if (not isinstance(source, str) or collections._reference_key(Path(root), source) != source
                    or entry.get("layout") not in ("markdown-items", "markdown-log")):
                _invalid()
    if marker["version"] == 2:
        for index, entry in enumerate(marker["collections"]):
            for other in marker["collections"][:index]:
                if _overlap(entry, other):
                    raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "collection ownership namespaces overlap")
    return marker


def marker_entry(collection_id, manifest_path, store_id, semantic_profile, source_path=None, layout=None):
    """One marker entry routing a collection to the store, with the profile it was created with.

    The marker is the routing authority, so a sweep that cannot read the store takes the
    collection's profile from here, never from its editable generated view.
    """
    return {"collection_id": collection_id, "manifest_path": manifest_path, "authority": "store",
            "store_id": store_id, "semantic_profile": semantic_profile,
            **({"source_path": source_path, "layout": layout} if source_path is not None else {})}


def _owned_regions(entry):
    from .. import records

    directory = Path(entry["manifest_path"]).parent
    return ((entry["manifest_path"], False),
            (entry["source_path"], entry["layout"] == "markdown-items"),
            ((directory / records._HELD_DIRECTORY).as_posix(), True),
            ((directory / "_history").as_posix(), True),
            ((directory / "_history.md").as_posix(), False))


def _overlap(entry, other):
    return any(_owns(entry, path) for path, _ in _owned_regions(other)) or any(
        _owns(other, path) for path, _ in _owned_regions(entry))


def file_ownership(manifest):
    """The namespace a file collection owns, in the shape a marker entry records it."""
    return {"manifest_path": manifest.path, "source_path": manifest.storage.source,
            "layout": manifest.storage.strategy}


def require_new_file_ownership(root, marker, entry):
    from . import ownership
    from .preview import unbound

    # File declarations retain authority before cutover, including when first enrollment has no routing marker.
    with unbound():
        proof = ownership.current(root)
        manifests, errors = collections.discover_collections_with_errors(
            root, authorize_path=lambda path: marker is None or selected_entry(root, marker, path) is None)
    # An incomplete declaration cannot prove separation; its owner must repair it before creating C.
    if any(proof.selected(root, error.path) is None and
           (marker is None or selected_entry(root, marker, error.path) is None) for error in errors):
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "file collection ownership is incomplete")
    for manifest in manifests:
        if _overlap(entry, file_ownership(manifest)):
            # Prevent taking a file collection's source or audit namespace; a false refusal costs another destination.
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "collection ownership namespaces overlap")


def _owns(entry, path):
    return _owns_key(_entry_regions(entry), collections._portable_path_key(path))


def _entry_regions(entry):
    return _region_keys(entry["manifest_path"], entry["source_path"], entry["layout"])


# One entry's namespace, folded once: recall asks about thousands of paths per call.
# The bound covers every entry of several markers; a miss only refolds one entry.
@functools.lru_cache(maxsize=256)
def _region_keys(manifest_path, source_path, layout):
    regions = _owned_regions({"manifest_path": manifest_path, "source_path": source_path, "layout": layout})
    keys = [(collections._portable_path_key(root), subtree) for root, subtree in regions]
    log = collections._portable_path_key(source_path) + "#" if layout == "markdown-log" else None
    return frozenset(key for key, _ in keys), tuple(key + "/" for key, subtree in keys if subtree), log


def _owns_key(regions, key):
    from .. import records

    exact, subtrees, log = regions
    if key in exact or key.startswith(subtrees):
        return True
    if log is not None and key.startswith(log):
        try:
            records._validate_item_key(key[len(log):])
            return True
        except collections.CollectionError:
            pass
    return False


def owned_entry(root, marker, path):
    """None means an ordinary file only when the marker completely names C ownership."""
    if marker["version"] != 2:
        # Legacy markers cannot distinguish arbitrary source paths; repair needs canonical custody, not editable views.
        raise CollectionStoreError("COLLECTION_STORE_MARKER_UPGRADE_REQUIRED", "canonical marker ownership needs an upgrade")
    path = collections._reference_key(Path(root), path)
    if path is None:
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "invalid canonical projection path")
    key = collections._portable_path_key(path)
    return next((entry for entry in marker["collections"] if _owns_key(_entry_regions(entry), key)), None)


VIEW_PATH = "COLLECTION_VIEW_PATH"


def refuse_view_paths(root, paths, refusal, *, expected_marker=None):
    """Protect canonical collection paths and their ancestors inside the caller's mutation boundary."""
    try:
        entry = _owning_entry(Path(root), paths, expected_marker=expected_marker)
    except CollectionStoreError as error:
        raise refusal(error.code, error.reason) from None
    if entry is not None:
        profile = entry.get("semantic_profile")
        tool = "its collection's tool" if profile is None else collection_profiles.profile_for(profile).tool
        raise refusal(VIEW_PATH, f"this path holds the views of a store collection; change its items with {tool}")


def _owning_entry(root, paths, *, expected_marker=None):
    from . import ownership

    paths = [path for path in (_knowledge_base_spelling(root, path) for path in paths)
             # Marker validation keeps every owned region under the knowledge base folder.
             if collections._reference_key(root, path) is not None]
    if not paths:
        return None
    if expected_marker is not None:
        try:
            raw = read_marker(root)
        except (held_fs.HeldFsError, OSError) as error:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "unreadable mode marker") from error
        if raw != expected_marker:
            # A file-only fallback retains the routing basis its acquisition proved.
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "mode marker changed after file authority was proved")
    return next((entry for path in paths if (entry := ownership.owned(root, path, ancestors=True)) is not None), None)


def _knowledge_base_spelling(root, path):
    # A case-folding volume opens another casing of the knowledge base folder as the folder itself.
    canonical = vault._canonical_kb_segment(path)
    return canonical if canonical == path or vault.vault_casefolds(root) else path


def upgraded_marker(root, conn, marker):
    """Derive ownership only from the validated current canonical declarations."""
    entries = []
    for entry in marker["collections"]:
        require_selected(conn, marker, entry, root=root)
        row = conn.execute("SELECT source_path,layout FROM collections WHERE collection_id=?",
                           (entry["collection_id"],)).fetchone()
        entries.append({**entry, "source_path": row[0], "layout": row[1]})
    result = {**marker, "version": 2, "authority_epoch": marker["authority_epoch"] + 1, "collections": entries}
    return parse_marker(root, json.dumps(result))


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
        "SELECT c.manifest_path,m.manifest_text,c.source_path,c.layout FROM collections c JOIN collection_manifests m "
        "ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version "
        "WHERE c.collection_id=?", (entry["collection_id"],),
    ).fetchone()
    # Missing canon cannot verify routing or profile; a false refusal delays C, while A/B remain available.
    if sid is None or sid[0] != marker["store_id"] or row is None or row[0] != entry["manifest_path"]:
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "store differs from authority marker")
    manifest = collections.parse_manifest_bytes(root, row[0], row[1].encode())
    if (row[2:] != (manifest.storage.source, manifest.storage.strategy) or
            marker["version"] == 2 and row[2:] != (entry["source_path"], entry["layout"])):
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "marker ownership differs from canonical storage")
    if entry.get("semantic_profile") is not None:
        if manifest.semantic_profile != entry["semantic_profile"]:
            # A false refusal costs a marker repair; accepting hides rows from profile sweeps.
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "marker profile differs from canonical manifest")


def require_marker(conn, marker, *, root):
    """Verify canonical bindings and reject projections outside the marker's declared ownership."""
    for entry in marker["collections"]:
        require_selected(conn, marker, entry, root=root)
    if marker["version"] == 1:
        marker = upgraded_marker(root, conn, marker)
    require_projection_ownership(conn, marker, root=root)


def require_projection_ownership(conn, marker, *, root):
    """Check projections against ownership that the current canonical marker already proved."""
    routed = {entry["collection_id"] for entry in marker["collections"]}
    for cid, path in conn.execute(
            "SELECT collection_id,path FROM projection_state UNION "
            "SELECT collection_id,view_path FROM items UNION SELECT collection_id,view_path FROM held_candidates"):
        if cid not in routed or path is None:
            continue
        owner = owned_entry(root, marker, path)
        if owner is None or owner["collection_id"] != cid:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "projection lies outside canonical ownership")


def required_state_compatibility_ids(root):
    """Trusted store enrollment requires this release's unified ownership and exposure boundary."""
    from .. import state_migration, state_paths
    from . import schema

    manifest = state_migration._load_manifest(state_paths.vault_state_dir(root), vault_root=root)
    owned = frozenset() if manifest is None else frozenset(manifest["descriptors"]) & state_migration._store_owned_ids()
    if owned:
        return owned | frozenset(schema.compatibility_ids())
    from ..vault_portability import checked_collection_replica

    with checked_collection_replica(root) as verified:
        if verified is not None:
            return state_migration._COLLECTION_STORE_COMPATIBILITY_IDS | frozenset(schema.compatibility_ids())
    return frozenset()


def _intent_collection(entry):
    # A migration's entry keeps the collection's file basis until its marker is installed.
    if (not isinstance(entry, dict) or set(entry) != {"collection_id", "manifest_path", "source_path", "txn_id", "basis"}
            or not all(isinstance(entry[name], str) and entry[name]
                       for name in ("collection_id", "manifest_path", "source_path", "basis"))
            or type(entry["txn_id"]) is not int or entry["txn_id"] < 1):
        _invalid()
    return entry


def _store_head(value):
    """Validate a store head an intent records: its commit sequence and chain hash, None only at genesis."""
    if (not isinstance(value, dict) or set(value) != {"commit_seq", "head_hash"}
            or type(value["commit_seq"]) is not int or value["commit_seq"] < 0
            or (value["head_hash"] is None) != (value["commit_seq"] == 0)
            or not (value["head_hash"] is None or isinstance(value["head_hash"], str) and value["head_hash"])):
        _invalid()


def _migrate_fields(intent):
    """Validate the fields a migration adds when it replaces an unadmitted store of its own (P1b.3c).

    ``supersedes`` names the store and generation a first enrolment's fence binds, which
    fresh staging replaces; ``base_head`` is the live store's head that a migration over an
    enrolled store imports onto, so a recovery can prove the store it restores.
    """
    if "supersedes" in intent:
        found = intent["supersedes"]
        if (intent["expected_marker"] is not None or not isinstance(found, dict)
                or set(found) != {"store_id", "generation"} or not isinstance(found["store_id"], str)
                or not found["store_id"] or type(found["generation"]) is not int or found["generation"] < 1):
            _invalid()
    if "base_head" in intent:
        if intent["expected_marker"] is None:
            _invalid()
        _store_head(intent["base_head"])


def _export_intent(intent):
    """Validate an export's intent: it retires ``expected_marker`` with its store and fence, so it has no target.

    ``verified``, once present, records that file mode read every collection as the store
    served it, so a rerun after the fence retire finishes without reading the files again.
    """
    # nosemgrep: ep-word-set -- the closed field names of the intent record this module writes.
    if (set(intent) - {"verified"} != {"version", "kind", "request_id", "plan_id", "expected_marker", "exported_at",
                                       "why", "collections"}
            or intent.get("verified", True) is not True
            or not all(isinstance(intent[name], str) and intent[name]
                       for name in ("request_id", "plan_id", "expected_marker", "exported_at", "why"))
            or not isinstance(intent["collections"], list) or not intent["collections"]):
        _invalid()
    for entry in intent["collections"]:
        if (not isinstance(entry, dict) or set(entry) != {"collection_id", "manifest_path", "source_path"}
                or not all(isinstance(value, str) and value for value in entry.values())):
            _invalid()
    if len(intent_collection_ids(intent)) != len(intent["collections"]):
        _invalid()
    return intent


def pending_create(conn):
    """Read the pending marker intent through its shared decoder."""
    row = conn.execute("SELECT value FROM store_meta WHERE key=?", (PENDING_CREATE,)).fetchone()
    return None if row is None else parse_pending_create(row[0])


def parse_pending_create(raw):
    """The one pending marker intent, with its closed ``kind`` and the ``collections`` it names.

    Version 1 is a create of one collection, as every release has written it. Version 2
    is a migration of existing file collections, each named with its import checkpoint,
    or an export of the routed collections back to files; a release without the export
    refuses its intent as invalid, so it never serves a store that an export owns.
    """
    try:
        intent = json.loads(raw)
        if isinstance(intent, dict) and type(intent.get("version")) is int and intent["version"] == 2 \
                and intent.get("kind") == EXPORT:
            return _export_intent(intent)
        if (type(intent["version"]) is not int or intent["version"] not in (1, 2)
                or not all(isinstance(intent[name], str) and intent[name]
                           for name in ("request_id", "target_marker"))
                or not (intent["expected_marker"] is None or isinstance(intent["expected_marker"], str))):
            _invalid()
        if intent["version"] == 1:
            if (not all(isinstance(intent[name], str) and intent[name] for name in
                        ("request_hash", "collection_id", "manifest_path", "source_path"))
                    or type(intent["txn_id"]) is not int or intent["txn_id"] < 1):
                _invalid()
            entry = {name: intent[name] for name in ("collection_id", "manifest_path", "source_path", "txn_id")}
            return {**intent, "kind": CREATE, "collections": [entry]}
        # Version 2 records migrations and exports; a create keeps writing version 1.
        if intent["kind"] != MIGRATE or not isinstance(intent["collections"], list) or not intent["collections"]:
            _invalid()
        entries = [_intent_collection(entry) for entry in intent["collections"]]
        if len({entry["collection_id"] for entry in entries}) != len(entries):
            _invalid()
        _migrate_fields(intent)
        return intent
    except (KeyError, TypeError, ValueError) as error:
        raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "invalid pending create intent") from error


def intent_collection_ids(intent):
    return {entry["collection_id"] for entry in intent["collections"]}


def require_intent_binding(conn, intent):
    """Prove the existing intent's immutable creation or import checkpoint; return its create receipt."""
    if intent["kind"] == EXPORT:
        return None
    if intent["kind"] == MIGRATE:
        for entry in intent["collections"]:
            if conn.execute(
                "SELECT 1 FROM txns t JOIN collections c ON c.collection_id=t.collection_id "
                "WHERE t.txn_id=? AND t.operation='legacy_import' AND t.legacy_event_json IS NULL "
                "AND c.collection_id=? AND c.manifest_path=? AND c.source_path=? AND c.created_txn=t.txn_id",
                (entry["txn_id"], entry["collection_id"], entry["manifest_path"], entry["source_path"]),
            ).fetchone() is None:
                raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "migration intent lacks its committed import checkpoint")
        return None
    row = conn.execute(
        "SELECT t.receipt_json FROM txns t JOIN collections c ON c.collection_id=t.collection_id "
        "WHERE t.txn_id=? AND t.operation='create' AND t.request_id=? AND t.request_hash=? "
        "AND c.collection_id=? AND c.manifest_path=? AND c.source_path=? AND c.created_txn=t.txn_id",
        (intent["txn_id"], intent["request_id"], intent["request_hash"], intent["collection_id"],
         intent["manifest_path"], intent["source_path"]),
    ).fetchone()
    if row is None:
        raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "create intent lacks its committed transaction")
    return json.loads(row[0])


def first_import_fence_matches(intent, target, fence):
    """The actual fence is this first import's target, its pre-cut generation, or its bound predecessor."""
    if (intent["kind"] != MIGRATE or intent["expected_marker"] is not None or "base_head" in intent):
        return False
    generation = target["collection_store_fence"]["generation"]
    predecessor = intent.get("supersedes")
    matching = (fence.enrolled and fence.store_id == target["store_id"] and fence.generation == generation)
    if predecessor is None:
        matching = matching or not fence.enrolled and generation == fence.generation + 1
    else:
        matching = generation == predecessor["generation"] + 1 and (matching or (
            fence.enrolled and fence.store_id == predecessor["store_id"]
            and fence.generation == predecessor["generation"]))
    return matching and (not fence.enrolled or fence.capability == target["collection_store_fence"]["capability"])


def marker_status(writer, intent):
    return intent_status(writer.root, writer.connection, intent)


def intent_status(root, conn, intent):
    """Whether a pending intent's marker is admitted, still pending, or in conflict; any connection reads it.

    An export admits no marker: its collections stay pending until it retires the store.
    """
    if intent["kind"] == EXPORT:
        return "pending"
    try:
        target_marker = parse_marker(root, intent["target_marker"])
        sid = conn.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
        if target_marker["store_id"] != sid or not all(any(
                routes(entry, pending["collection_id"], pending["manifest_path"], sid)
                for entry in target_marker["collections"]) for pending in intent["collections"]):
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
    from . import ownership
    from .preview import production_bound

    proof = ownership.current(writer.root, conn=writer.connection, private=not production_bound(writer.root))
    path = projection.get("path")
    owner = proof.owner(writer.root, path) if isinstance(path, str) else None
    return (owner is not None and owner["collection_id"] == projection["collection_id"]
            and owner["collection_id"] in proof.admitted)


def collection_admitted(writer, collection_id):
    from ..cli_ops import OpError
    from . import ownership
    from .preview import production_bound

    try:
        return collection_id in ownership.current(writer.root, conn=writer.connection,
            private=not production_bound(writer.root)).admitted
    except (held_fs.HeldFsError, OSError, CollectionStoreError, RuntimeError, OpError):
        return False


def orphan_cleanup_eligible(writer, directory):
    """A directory sweep cannot attribute an unjournaled file to its trigger.

    Pending creation delays only ambiguous orphan cleanup in its view subtree;
    identified neighboring projections can still recover and publish normally.
    """
    from . import ownership
    from .preview import production_bound

    proof = ownership.current(writer.root, conn=writer.connection, private=not production_bound(writer.root))
    directory = collections._portable_path_key(directory.as_posix())
    for cid, manifest, source, layout in writer.connection.execute(
        "SELECT collection_id,manifest_path,source_path,layout FROM collections"
    ):
        if cid in proof.admitted:
            continue
        source = Path(source)
        protected = (Path(manifest).parent, source.parent if layout == "markdown-log" else source)
        if any(directory == (key := collections._portable_path_key(parent.as_posix()))
               or directory.startswith(key + "/") for parent in protected):
            return False
    return True
