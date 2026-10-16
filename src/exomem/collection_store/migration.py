"""The declared migration of a vault's file Records and Planning collections into the store.

OpenSpec move-structured-collections-to-sqlite P1b.3 and P1b.3b, design §10 and §16 A5.
``prepare`` imports and proves every file collection into retained private staging under
the vault's state directory, beside a live writer and without the lease. ``cutover`` runs
in the owner's offline session: it carries that staging forward when nothing it was built
from changed, or rebuilds it, then makes it the live store and admits it in the A5 order:
publish the store, cut the coordinator's store fence, reacquire the lease, record the
compatibility descriptor, publish the replica and install the marker. Both reuse the
preflight's capture, import and proof, and the admission owner's create machinery
through the one pending intent. Vault files are never rewritten or deleted, and imported
rows stay json-v1 as ``legacy_import`` writes them. Both refuse until the release enables
``collections-migrate-v1``.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from .. import reserved_paths, state_paths
from . import (
    admission,
    authority,
    capability,
    chain,
    connection,
    legacy_import,
    preflight,
    schema,
    snapshot,
    takeover,
)
from .connection import CollectionStoreError

STAGING_PREFIX = ".exomem-collection-migration-"
_DESCRIPTOR = "collection-migration"
_STAGING = "staging.sqlite"
#: The staging store's record of what it was built from; dropped before it goes live.
_BASIS = "migration_basis"
_ACTOR = "collections-store-migration"


def _staging_directories(root):
    state = state_paths.vault_state_dir(root)
    if not state.is_dir():
        return []
    return sorted(path for path in state.iterdir()
                  if reserved_paths.classify_logical(path.name).descriptor_id == _DESCRIPTOR
                  and path.is_dir() and not path.is_symlink())


def _discard(directory):
    shutil.rmtree(directory)


def _new_directory(root):
    directory = state_paths.ensure_vault_state_dir(root) / f"{STAGING_PREFIX}{uuid.uuid4().hex}"
    directory.mkdir(mode=0o700)
    return directory


def _scratch(root):
    """The private directory for the transient audit spool and store copy, beside the live store.

    Their own registered families place them there; inside the staging directory they
    would overlap its family.
    """
    return state_paths.ensure_vault_state_dir(root)


def _live_snapshot(root, directory):
    """Copy the live store into ``directory`` as the staging base; what the copy was taken from."""
    with snapshot.staged_snapshot(root, directory=_scratch(root),
                                  deadline=time.monotonic() + preflight.DEADLINE_SECONDS) as copied:
        copied.path.rename(directory / _STAGING)
        return {"store_id": copied.store_id, "instance_id": copied.instance_id, "commit_seq": copied.commit_seq,
                "head_hash": copied.head_hash, "snapshot_sha256": copied.file_sha256}


def _live_digest(root):
    """The digest of a fresh validated copy of the live store, compared to the staging base's."""
    with snapshot.staged_snapshot(root, directory=_scratch(root),
                                  deadline=time.monotonic() + preflight.DEADLINE_SECONDS) as copied:
        return copied.file_sha256


def _read_basis(conn):
    row = conn.execute("SELECT value FROM store_meta WHERE key=?", (_BASIS,)).fetchone()
    return None if row is None else json.loads(row[0])


def _write_basis(conn, basis):
    conn.execute("INSERT OR REPLACE INTO store_meta(key,value) VALUES (?,?)",
                 (_BASIS, json.dumps(basis, sort_keys=True, separators=(",", ":"))))


def _recorder(staging):
    def record(captured, result):
        manifest = captured.manifest
        basis = _read_basis(staging)
        basis["collections"][manifest.collection_id] = {
            "manifest_path": manifest.path, "source_path": manifest.storage.source,
            "layout": manifest.storage.strategy, "semantic_profile": manifest.semantic_profile,
            "basis": captured.collection_basis, "txn_id": result.checkpoint_txn_id,
            "rows": result.item_count, "legacy_events": result.legacy_event_count,
            "legacy_audit_status": captured.legacy_inspection["status"]}
        _write_basis(staging, basis)
    return record


def _require_stageable(root):
    """Refuse to stage beside a store that this migration cannot extend; the pending intent, if any.

    A copy would carry another unfinished intent into staging, and the migration's own
    intent would replace it, losing that create's routing. A store must also have the
    marker that routes it, and a marker its local store. A false refusal costs the owner
    one finished create, store opening or replica adoption before migrating.
    """
    path = connection.store_path(root)
    marker = authority.read_marker(root)
    if not path.exists():
        if marker is not None:
            raise CollectionStoreError(takeover.SYNC_PENDING, "this vault routes collections to a store it has "
                                       "not opened here; adopt its replica, then migrate")
        return None
    with closing(connection.open_reader(path)) as reader:
        intent = authority.pending_create(reader)
    if intent is not None and intent["kind"] != authority.MIGRATE:
        raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT",
                                   "a collection create is still pending; finish it before migrating")
    if intent is None and marker is None:
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT",
                                   "a collection store exists here without its authority marker")
    return intent


def _stage(root):
    """Build fresh staging from the live store (if any) and every file collection's import and proof.

    Returns the report and the retained staging directory, or None when nothing is ready
    or a collection is blocked: a failed proof leaves no staging behind.
    """
    for old in _staging_directories(root):
        _discard(old)
    rows, pending, store_ids = preflight.candidates(root)
    directory = _new_directory(root)
    try:
        live = _live_snapshot(root, directory) if connection.store_path(root).exists() else None
        if pending:
            with connection.staging_store(directory / _STAGING) as staging:
                staging.execute("BEGIN IMMEDIATE")
                _write_basis(staging, {"version": 1, "live": live, "collections": {}})
                staging.execute("COMMIT")
                rows.extend(preflight.prove_into(root, pending, staging, _scratch(root),
                                                 preflight.import_context(_ACTOR), record=_recorder(staging)))
        report = preflight.report(rows, store_ids)
    except BaseException:
        _discard(directory)
        raise
    if report["verdict"] != "migratable":
        _discard(directory)
        return report, None
    return report, directory


#: Why an apply rebuilt its staging, as the report names it: closed reasons of this module.
NO_STAGING, SEVERAL_STAGINGS, STAGING_MISSING = "no_staging", "several_stagings", "staging_missing"
INTERRUPTED, NO_BASIS, LIVE_CHANGED = "interrupted_cutover", "no_basis", "live_store_changed"
SET_CHANGED, AUDIT_UNREADABLE, BASIS_CHANGED = "collection_set_changed", "audit_unreadable", "basis_changed"


def _carried(root, directory):
    """The report for staging that still matches every input it was built from, or why it does not.

    Each collection is compared by its own basis, so an unrelated activity-log write does
    not invalidate it; the live store, when one exists, must be unchanged since its copy.
    Returns the report, or None with the reason and the paths of changed collections.
    """
    path = directory / _STAGING
    if not path.is_file():
        return None, STAGING_MISSING, []
    try:
        with closing(connection.open_reader(path)) as reader:
            basis = _read_basis(reader)
            unfinished = authority.pending_create(reader) is not None
    except (CollectionStoreError, sqlite3.DatabaseError, ValueError):
        # Staging that cannot be read is rebuilt, never trusted.
        return None, NO_BASIS, []
    if unfinished:
        return None, INTERRUPTED, []
    if basis is None or basis.get("version") != 1:
        return None, NO_BASIS, []
    live = connection.store_path(root).exists()
    if (basis["live"] is not None) != live or (
            live and _live_digest(root) != basis["live"]["snapshot_sha256"]):
        return None, LIVE_CHANGED, []
    rows, pending, store_ids = preflight.candidates(root)
    recorded = basis["collections"]
    if rows and any(row["status"] == "blocked" for row in rows) or {
            manifest.collection_id: manifest.path for manifest in pending} != {
            cid: entry["manifest_path"] for cid, entry in recorded.items()}:
        return None, SET_CHANGED, []
    changed = []
    with preflight.scanned(root, _scratch(root), pending) as (audit, blocked):
        if audit is None:
            return None, AUDIT_UNREADABLE, []
        for manifest in pending:
            try:
                captured = legacy_import.capture_legacy_collection(root, manifest.path, audit=audit)
            except (TimeoutError, *preflight._REFUSALS):
                changed.append(manifest.path)
                continue
            entry = recorded[manifest.collection_id]
            if captured.collection_basis != entry["basis"]:
                changed.append(manifest.path)
                continue
            rows.append(preflight._row(manifest.path, manifest, status="ready", proof="passed",
                                       rows=entry["rows"], legacy_events=entry["legacy_events"],
                                       legacy_audit_status=entry["legacy_audit_status"]))
    if changed:
        return None, BASIS_CHANGED, sorted(changed)
    return preflight.report(rows, store_ids), None, []


def dry_run(root) -> dict:
    """The read-only preflight; the vault and its state stay untouched."""
    return {**preflight.preflight(root), "step": "dry-run"}


def prepare(vault_root) -> dict:
    """Pre-import and prove every file Records and Planning collection into retained staging.

    It runs beside a live writer: it reads vault files and copies the live store with the
    backup API, takes no lease and writes only its own staging under the state directory.
    A later ``cutover`` carries it forward when nothing it read has changed.
    """
    admission.require_owner(vault_root, "the collections-store migration")
    capability.require_collections_migrate()
    root = Path(vault_root).resolve()
    coordinator = preflight.coordinator_capability()
    if _require_stageable(root) is not None:
        raise CollectionStoreError("COLLECTION_STORE_LEASE_REQUIRED",
                                   "a migration cutover is unfinished; run the apply step to finish it")
    report, directory = _stage(root)
    return {**report, "step": "prepare", "coordinator": coordinator,
            "staging": "retained" if directory is not None else "none"}


def _require_current_coordinator(runtime, fence_client, marker, staging):
    """Refuse before any live change unless the coordinator's fence and head fit the staging store.

    Without a marker the fence must be unenrolled, so the first cut succeeds; with one the
    fence must bind its store, the store must record no divergence, and the head the
    coordinator recorded must be in the staging chain from this instance. This prevents installing a store whose admission can
    never finish; a false refusal costs the owner a retry once the vault replica arrives.
    """
    fence = fence_client.collection_store_fence()
    if marker is None:
        if fence.enrolled or fence.generation != 0:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT",
                                       "the coordinator already fences a store for this vault, which has no marker")
        return
    admission._require_fence(fence, marker)
    meta = dict(staging.execute("SELECT key,value FROM store_meta"))
    if schema.META_REPLICA_DIVERGENCE in meta:
        raise CollectionStoreError(takeover.DIVERGED, "this store records unresolved divergence; "
                                   "run adopt-local, then migrate")
    recorded = admission._acquired(runtime, fence, "COLLECTION_STORE_LEASE_REQUIRED").collection_store_head
    if recorded is None:
        return
    if (recorded.store_id != meta[schema.META_STORE_ID] or recorded.instance_id != meta[schema.META_INSTANCE_ID]
            or chain.head_relation(staging, recorded.commit_seq, recorded.head_hash) not in ("same", "ancestor")):
        raise CollectionStoreError(takeover.SYNC_PENDING, "the coordinator's recorded head is not in this store; "
                                   "wait for the vault replica, open the store, then migrate again")


def _intent(root, marker, raw, staging):
    """The migration intent and target marker for a verified staging store."""
    sid = staging.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    recorded = _read_basis(staging)["collections"]
    entries = [authority.marker_entry(cid, entry["manifest_path"], sid, entry["semantic_profile"],
                                      entry["source_path"], entry["layout"])
               for cid, entry in sorted(recorded.items(), key=lambda item: item[1]["manifest_path"])]
    target = admission.target_marker(root, marker, sid, entries)
    for entry in entries:
        # Prevents a migrated collection from taking a file collection's namespace; a false
        # refusal leaves the vault in files until the overlap is repaired.
        authority.require_new_file_ownership(root, target, entry)
    return {"version": 2, "kind": authority.MIGRATE, "request_id": "migrate:" + uuid.uuid4().hex,
            "collections": [{"collection_id": entry["collection_id"], "manifest_path": entry["manifest_path"],
                             "source_path": entry["source_path"], "txn_id": recorded[entry["collection_id"]]["txn_id"]}
                            for entry in entries],
            "expected_marker": None if raw is None else raw.decode("utf-8"),
            "target_marker": json.dumps(target, sort_keys=True, separators=(",", ":"))}


def _seal(directory, intent):
    """Record the intent in the staging store and close it journal-free, ready to rename into place."""
    with connection.staging_store(directory / _STAGING) as staging:
        staging.execute("BEGIN IMMEDIATE")
        staging.execute("DELETE FROM store_meta WHERE key=?", (_BASIS,))
        staging.execute("INSERT INTO store_meta(key,value) VALUES (?,?)",
                        (authority.PENDING_CREATE, json.dumps(intent, sort_keys=True, separators=(",", ":"))))
        staging.execute("COMMIT")
        chain.verify_store_chain(staging)
        if staging.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
            raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store is still open")
    if any((directory / (_STAGING + suffix)).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store is still open")


def cutover(session, manager, fence_client) -> dict:
    """Make verified staging the live store and admit it, inside the owner's offline session.

    A live store carrying this migration's intent (a cutover that stopped after it
    published the store) resumes without importing again. Otherwise, under the mutation
    boundary, staging is carried forward or rebuilt; a blocked collection leaves no store,
    no marker and an unenrolled fence, and the report names it with its failed check.
    """
    capability.require_collections_migrate()
    root = session.root
    session.verify_custody()
    admission._ProducerSession.bind(session, manager)
    admission._bind_fence_client(session, fence_client)
    coordinator = preflight.coordinator_capability(fence_client)
    with manager.consistency_guard(root, operation="collection_store_migrate"):
        if _require_stageable(root) is not None:
            result = admission._resume_locked(session, fence_client)
            for leftover in _staging_directories(root):
                _discard(leftover)
            return {"mode": "collections-store", "step": "apply", "coordinator": coordinator,
                    "staging": "resumed", **result}
        raw = authority.read_marker(root)
        marker = None if raw is None else authority.parse_marker(root, raw)
        runtime = session.runtime() if session.path.exists() else None
        with manager.writer_authority_guard(vault_root=root):
            token = manager._fencing_token
            directories = _staging_directories(root)
            report, reason, changed = _carried(root, directories[0]) if len(directories) == 1 else (
                None, NO_STAGING if not directories else SEVERAL_STAGINGS, [])
            directory = directories[0] if report is not None else None
            if report is None:
                report, directory = _stage(root)
            if directory is None:
                return {**report, "step": "apply", "coordinator": coordinator, "staging": "none"}
            with closing(connection.open_reader(directory / _STAGING)) as staging:
                _require_current_coordinator(runtime, fence_client, marker, staging)
                intent = _intent(root, marker, raw, staging)
            _seal(directory, intent)
            if runtime is not None and not runtime.retire_idle_handle():
                raise CollectionStoreError(takeover.BUSY, "the live store is still borrowed")
            session.require(token)
            takeover.replace_live(session, _STAGING, lambda: session.require(token), subdirectory=directory.name)
        result = admission._resume_locked(session, fence_client, preparation_token=token)
        _discard(directory)
    return {**report, "step": "apply", "coordinator": coordinator,
            "staging": "carried_forward" if reason is None else "rebuilt",
            **({"rebuilt_because": reason, "changed": changed} if reason is not None else {}), **result}
