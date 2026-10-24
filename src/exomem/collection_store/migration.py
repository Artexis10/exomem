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

import hashlib
import json
import logging
import sqlite3
import time
import uuid
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

from .. import held_fs, reserved_paths, state_paths
from ..cli_ops import OpError
from . import (
    admission,
    authority,
    capability,
    chain,
    connection,
    handoff_state,
    legacy_import,
    legacy_originals,
    owner,
    ownership,
    preflight,
    replica,
    schema,
    snapshot,
    takeover,
)
from .connection import CollectionStoreError

logger = logging.getLogger(__name__)

STAGING_PREFIX = ".exomem-collection-migration-"
_STAGING = "staging.sqlite"
#: The staging store's record of what it was built from; dropped before it goes live.
_BASIS = "migration_basis"
_ACTOR = "collections-store-migration"


def _staging_directories(root):
    return connection.migration_staging_directories(root)


@contextmanager
def _migration_scope(root, deadline=None):
    reserved_paths.warm_identity_catalogue_before_boundary(root)
    try:
        with reserved_paths._subsystem_authority_scope("collection_store.migration"), \
                reserved_paths._identity_coordination_scope(root, descriptor_ids=(connection.MIGRATION_STAGING,),
                                                           deadline=deadline):
            _check_deadline(deadline)
            yield
    except OpError:
        _check_deadline(deadline)
        raise


@contextmanager
def _held_workspace(directory, *, create=False):
    with held_fs.acquire(directory.parent.parent).require() as filesystem, \
            filesystem.parent(directory.parent.name, access="mutate").require() as state, \
            filesystem.parent(f"{directory.parent.name}/{directory.name}", create=create, exclusive=create,
                              access="mutate").require() as staging:
        def check():
            try:
                filesystem.validate_directory(state).require()
                filesystem.validate_directory(staging).require()
            except held_fs.HeldFsError as error:
                raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging directory changed during migration") from error
        check()
        if create:
            filesystem.flush_directory(state).require()
        yield filesystem, state, staging, check


@contextmanager
def _held_staging(directory, leaf=_STAGING):
    """Keep the actual state, staging and database identities through comparison and installation."""
    with _held_workspace(directory) as (filesystem, _state, staging, parent_check), \
            filesystem.file(staging, leaf).require() as database:
        def check():
            parent_check()
            try:
                with filesystem.file(staging, leaf).require() as named:
                    if not held_fs._same_file_identity(named.identity, database.identity):
                        raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store changed during cutover")
            except held_fs.HeldFsError as error:
                raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store changed during cutover") from error
        check()
        yield filesystem, staging, database, check


def _single_file(conn, directory):
    return (conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
            and not any((directory / (_STAGING + suffix)).exists() for suffix in snapshot._COMPANIONS))


def _proved_version(directory, database):
    with closing(connection.open_reader(directory / _STAGING, busy_timeout_ms=0)) as reader:
        if not _single_file(reader, directory):
            raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store requires SQLite companions")
    return database.version()


def _discard(directory):
    """Remove only a recognized, closed staging database; preserve unknown or changed entries."""
    try:
        with held_fs.acquire(directory.parent.parent).require() as filesystem, \
                filesystem.parent(f"{directory.parent.name}/{directory.name}", access="mutate").require() as staging:
            if not set(filesystem.iter_names(staging)):
                filesystem.unlink_directory(staging).require()
                return True
        with _held_staging(directory) as (filesystem, staging, database, check):
            if set(filesystem.iter_names(staging)) != {_STAGING}:
                return False
            version = database.version()
            with closing(connection.open_reader(directory / _STAGING, busy_timeout_ms=0)) as reader:
                if not _single_file(reader, directory):
                    return False
                basis = _read_basis(reader)
                pending = authority.pending_create(reader)
                if basis is None and (pending is None or pending["kind"] != authority.MIGRATE):
                    return False
                chain.verify_store_chain(reader)
            check()
            if database.version() != version:
                return False
            with filesystem.file(staging, _STAGING, access="mutate").require() as current:
                if current.identity != database.identity or current.version() != version:
                    return False
                filesystem.unlink(current).require()
            filesystem.unlink_directory(staging).require()
            return True
    except (OSError, ValueError, held_fs.HeldFsError, sqlite3.DatabaseError, CollectionStoreError, chain.StoreChainError):
        return False


def _check_deadline(deadline):
    if _expired(deadline):
        raise TimeoutError("COLLECTION_MIGRATION_DEADLINE: migration deadline elapsed")



def _scratch(root):
    """The private directory for the transient audit spool and store copy, beside the live store.

    Their own registered families place them there; inside the staging directory they
    would overlap its family.
    """
    return state_paths.ensure_vault_state_dir(root)


def _deadline(deadline):
    """``deadline`` (absolute monotonic), or the preflight's own from now."""
    return time.monotonic() + preflight.DEADLINE_SECONDS if deadline is None else deadline


def _expired(deadline):
    """Whether a handoff's self-imposed deadline has passed; an owner's apply has none."""
    return deadline is not None and time.monotonic() >= deadline


def _copied(copied):
    return {"store_id": copied.store_id, "instance_id": copied.instance_id, "commit_seq": copied.commit_seq,
            "head_hash": copied.head_hash, "snapshot_sha256": copied.file_sha256}


#: The head of a copied live store, which a basis records beside the copy's digest.
_HEAD = ("store_id", "instance_id", "commit_seq", "head_hash")


def _live_copy(root, deadline=None):
    """The head and digest of a fresh validated copy of the live store, compared to the staging base's."""
    with snapshot.staged_snapshot(root, directory=_scratch(root), deadline=_deadline(deadline)) as copied:
        return _copied(copied)


def _basis_text(conn):
    row = conn.execute("SELECT value FROM store_meta WHERE key=?", (_BASIS,)).fetchone()
    return None if row is None else row[0]


def _read_basis(conn):
    raw = _basis_text(conn)
    return None if raw is None else json.loads(raw)


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
            "legacy_audit_status": captured.legacy_inspection["status"],
            "material_version": legacy_import.MATERIAL_VERSION, "material_digest": result.material_digest}
        _write_basis(staging, basis)
    return record


#: The fields ``_recorder`` writes per collection; a basis missing one is rebuilt, never read.
_RECORDED = frozenset({"manifest_path", "source_path", "layout", "semantic_profile", "basis", "txn_id", "rows",
                       "legacy_events", "legacy_audit_status", "material_version", "material_digest"})


def _usable(basis):
    try:
        return (basis["version"] == 2
                and (basis["live"] is None or (isinstance(basis["live"]["snapshot_sha256"], str)
                                               and set(_HEAD) <= basis["live"].keys()))
                and all(isinstance(entry, dict) and _RECORDED <= entry.keys()
                        for entry in basis["collections"].values()))
    except (KeyError, TypeError, AttributeError):
        return False


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
    # A cutover an earlier release left stays at its schema until the resume's writer migrates it.
    with closing(connection.open_head_reader(path)) as reader:
        intent = authority.pending_create(reader)
    if intent is not None and intent["kind"] == authority.EXPORT:
        raise admission.export_pending()
    if intent is not None and intent["kind"] != authority.MIGRATE:
        raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT",
                                   "a collection create is still pending; finish it before migrating")
    if intent is None and marker is None:
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT",
                                   "a collection store exists here without its authority marker")
    return intent


def _stage(root, *, base=True, deadline=None, donor=None, ownership_proof=None):
    """Assemble a fresh candidate, retaining only eligible, unchanged proved legacy material."""
    rows, pending, store_ids = preflight.candidates(root, ownership_proof=ownership_proof)
    if not pending:
        return preflight.report(rows, store_ids), None
    directory = (state_paths.ensure_vault_state_dir(root) / f"{STAGING_PREFIX}{uuid.uuid4().hex}"
                 if donor is None else donor)
    # A stopped attempt may leave unknown bytes; a new private leaf preserves them without blocking a retry.
    leaf = _STAGING if donor is None else f"refresh-{uuid.uuid4().hex}.sqlite"
    candidate = directory / leaf
    installed = False
    # The donor stays held and untouched until the entire replacement is valid and closed.
    with ExitStack() as stack, ExitStack() as sql:
        candidate_file = None
        try:
            with _migration_scope(root, deadline):
                filesystem, state, parent, workspace_check = stack.enter_context(
                    _held_workspace(directory, create=donor is None)
                )
                donor_reader, basis, donor_check = None, None, lambda: None
                if donor is not None:
                    database = stack.enter_context(filesystem.file(parent, _STAGING).require())
                    donor_reader = stack.enter_context(connection.reader_snapshot(donor / _STAGING, busy_timeout_ms=0))
                    basis = _read_basis(donor_reader)
                    if basis is None or not _usable(basis) or authority.pending_create(donor_reader) is not None:
                        basis = None
                    donor_version = database.version()
                    def donor_check():
                        with filesystem.file(parent, _STAGING).require() as current:
                            if current.identity != database.identity or current.version() != donor_version:
                                raise CollectionStoreError("COLLECTION_STORE_BUSY", "the donor staging store changed during refresh")
                def check():
                    _check_deadline(deadline)
                    workspace_check()
                    donor_check()
                    if candidate_file is not None:
                        with filesystem.file(parent, leaf).require() as current:
                            if not held_fs._same_file_identity(candidate_file.identity, current.identity):
                                raise CollectionStoreError("COLLECTION_STORE_BUSY", "the replacement staging store changed during refresh")
                check()
                live = None
                if base and connection.store_path(root).exists():
                    with snapshot.staged_snapshot(root, directory=_scratch(root), deadline=_deadline(deadline),
                                                  cancelled=lambda: check() or False) as copied:
                        with filesystem.file(state, copied.path.name, access="mutate").require() as source:
                            check()
                            # The snapshot already proved and closed this live base; move its one file into our workspace.
                            filesystem.rename(source, parent, leaf, replace=False).require()
                            live = _copied(copied)
                    candidate_file = stack.enter_context(filesystem.file(parent, leaf, access="mutate").require())
                else:
                    candidate_file = stack.enter_context(filesystem.file(parent, leaf, create=True, exclusive=True,
                                                                          access="mutate").require())
                staging = sql.enter_context(connection.staging_store(candidate))
                sql.enter_context(legacy_import.checked_sql(staging, check))
                # Retain SQLite custody while authorization reads release identity coordination; cleanup skips busy candidates.
                if staging.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone() != ("exclusive",):
                    raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store cannot retain its writer lock")
                staging.execute("BEGIN IMMEDIATE")
                _write_basis(staging, {"version": 2, "live": live, "collections": {}})
                staging.execute("COMMIT")
            context = preflight.import_context(_ACTOR)
            with preflight.scanned(root, _scratch(root), pending, deadline=deadline) as (audit, blocked), \
                    legacy_import.presentation_context(root, conn=staging, ownership_proof=ownership_proof,
                                                       manifests=pending) as presentation:
                if audit is None:
                    rows.extend(blocked)
                else:
                    for manifest in pending:
                        check()
                        try:
                            captured = legacy_import.capture_legacy_collection(
                                root, manifest.path, audit=audit, presentation_operation=presentation
                            )
                        except preflight._REFUSALS as error:
                            rows.append(preflight._row(manifest.path, manifest, status="blocked", blocker=preflight._blocker(error)))
                            continue
                        try:
                            with _migration_scope(root, deadline):
                                check()
                                entry = None if basis is None else basis["collections"].get(manifest.collection_id)
                                material = None
                                if entry is not None and captured.collection_basis == entry["basis"]:
                                    try:
                                        with legacy_import.checked_sql(donor_reader, check):
                                            material = legacy_import.retained_material(donor_reader, entry, check=check)
                                    except (ValueError, TypeError, KeyError, sqlite3.DatabaseError):
                                        material = None
                                staging.execute("BEGIN IMMEDIATE")
                                result = (legacy_import.assemble(staging, material, check) if material is not None else
                                          legacy_import.import_legacy_collection(staging, captured, audit=audit,
                                                                                 context=context, defer_presentation=True))
                            # Governance sidecars take their own identity domains; the SQL changes remain uncommitted here.
                            legacy_import._recheck(captured, check=check)
                            with _migration_scope(root, deadline):
                                check()
                                legacy_import._recheck_sources(captured, check=check)
                                _recorder(staging)(captured, result)
                                staging.execute("COMMIT")
                        except preflight._REFUSALS as error:
                            rows.append(preflight._row(manifest.path, manifest, status="blocked", blocker=preflight._blocker(error)))
                            continue
                        finally:
                            if staging.in_transaction:
                                with _migration_scope(root):
                                    staging.execute("ROLLBACK")
                        rows.append(preflight._row(manifest.path, manifest, status="ready", proof="passed",
                            rows=result.item_count, legacy_events=result.legacy_event_count,
                            legacy_audit_status=captured.legacy_inspection["status"]))
            with _migration_scope(root, deadline):
                check()
                chain.verify_store_chain(staging, check=check)
                if staging.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise CollectionStoreError("COLLECTION_STORE_BUSY", "the refreshed staging store has invalid references")
                if staging.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                    raise CollectionStoreError("COLLECTION_STORE_BUSY", "the refreshed staging store is still open")
                sql.close()
                report = preflight.report(rows, store_ids)
                check()
                if any((directory / (leaf + suffix)).exists() for suffix in snapshot._COMPANIONS):
                    raise CollectionStoreError("COLLECTION_STORE_BUSY", "the refreshed staging store is still open")
                if report["verdict"] != "migratable":
                    return report, None
                if donor is not None:
                    donor_reader.close()
                    donor_check()
                    filesystem.rename(candidate_file, parent, _STAGING, replace=True).require()
                    filesystem.flush_directory(parent).require()
                installed = True
                return report, directory
        finally:
            if candidate_file is not None:
                with _migration_scope(root):
                    sql.close()
                    if not installed:
                        # This attempt owns this exact file; held unlink preserves a replacement at its name.
                        filesystem.unlink(candidate_file)
                        if donor is None and not set(filesystem.iter_names(parent)):
                            # Only this attempt's freshly created, now-empty directory can be removed here.
                            with held_fs.acquire(directory.parent).require() as state_fs, \
                                    state_fs.parent(directory.name, access="mutate").require() as empty:
                                state_fs.unlink_directory(empty).require()


#: Why an apply rebuilt its staging, as the report names it: closed reasons of this module.
NO_STAGING, SEVERAL_STAGINGS, STAGING_MISSING = "no_staging", "several_stagings", "staging_missing"
INTERRUPTED, NO_BASIS, LIVE_CHANGED = "interrupted_cutover", "no_basis", "live_store_changed"
SET_CHANGED, AUDIT_UNREADABLE, BASIS_CHANGED = "collection_set_changed", "audit_unreadable", "basis_changed"
#: The live store kept its head since the copy, but not its bytes: the serving worker wrote
#: bookkeeping, such as a view it checked or published, which a swap of the old copy would lose.
LIVE_REWRITTEN = "live_store_rewritten"


def _carried(root, directory, *, supersede=False, deadline=None, verified=None, ownership_proof=None):
    """The report for staging that still matches every input it was built from, or why it does not.

    Each collection is compared by its own basis, so an unrelated activity-log write does
    not invalidate it; the live store, when one exists, must be unchanged since its copy:
    its head (``LIVE_CHANGED``) and its bytes (``LIVE_REWRITTEN``). ``supersede`` ignores a
    live store that is this migration's own unadmitted one. Returns the report, or None
    with the reason and the paths of changed collections.
    """
    path = directory / _STAGING
    with ExitStack() as inputs:
        with _migration_scope(root, deadline):
            if not path.is_file():
                return None, STAGING_MISSING, []
            try:
                _filesystem, _parent, database, staging_check = inputs.enter_context(_held_staging(directory))
                proved_version = database.version()
                reader = inputs.enter_context(connection.reader_snapshot(path, busy_timeout_ms=0))
                if not _single_file(reader, directory):
                    return None, NO_BASIS, []
                basis_text = _basis_text(reader)
                basis = None if basis_text is None else json.loads(basis_text)
                unfinished = authority.pending_create(reader) is not None
            except (OSError, held_fs.HeldFsError, CollectionStoreError, sqlite3.DatabaseError, ValueError):
                # Staging that cannot be read is rebuilt, never trusted.
                return None, NO_BASIS, []
        if unfinished:
            return None, INTERRUPTED, []
        if basis is None or not _usable(basis):
            return None, NO_BASIS, []
        live = connection.store_path(root).exists() and not supersede
        if (basis["live"] is not None) != live:
            return None, LIVE_CHANGED, []
        if live:
            copied = _live_copy(root, deadline)
            if any(copied[name] != basis["live"][name] for name in _HEAD):
                return None, LIVE_CHANGED, []
            if copied["snapshot_sha256"] != basis["live"]["snapshot_sha256"]:
                return None, LIVE_REWRITTEN, []
        rows, pending, store_ids = preflight.candidates(root, ownership_proof=ownership_proof)
        recorded = basis["collections"]
        if rows and any(row["status"] == "blocked" for row in rows) or {
                manifest.collection_id: manifest.path for manifest in pending} != {
                cid: entry["manifest_path"] for cid, entry in recorded.items()}:
            return None, SET_CHANGED, []
        changed = []
        def check():
            _check_deadline(deadline)
            staging_check()
        with preflight.scanned(root, _scratch(root), pending, deadline=deadline) as (audit, blocked), \
                legacy_import.presentation_context(root, conn=reader, ownership_proof=ownership_proof,
                                                   manifests=pending) as presentation:
            if audit is None:
                return None, AUDIT_UNREADABLE, []
            for manifest in pending:
                check()
                try:
                    captured = legacy_import.capture_legacy_collection(
                        root, manifest.path, audit=audit, presentation_operation=presentation
                    )
                except (TimeoutError, *preflight._REFUSALS):
                    changed.append(manifest.path)
                    continue
                entry = recorded[manifest.collection_id]
                try:
                    with legacy_import.checked_sql(reader, check):
                        material = legacy_import.retained_material(reader, entry, check=check)
                except (ValueError, TypeError, KeyError, sqlite3.DatabaseError):
                    material = None
                if captured.collection_basis != entry["basis"] or material is None:
                    changed.append(manifest.path)
                    continue
                rows.append(preflight._row(manifest.path, manifest, status="ready", proof="passed",
                                           rows=entry["rows"], legacy_events=entry["legacy_events"],
                                           legacy_audit_status=entry["legacy_audit_status"]))
        with _migration_scope(root, deadline):
            check()
            # The witness predates all comparisons; a fresh pathname or version cannot certify old inputs.
            if database.version() != proved_version or not _single_file(reader, directory):
                raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging bytes changed during comparison")
            if changed:
                return None, BASIS_CHANGED, sorted(changed)
            if verified is not None:
                verified["basis_text"] = basis_text
            return preflight.report(rows, store_ids), None, []



def dry_run(root) -> dict:
    """The read-only preflight; the vault and its state stay untouched."""
    return {**preflight.preflight(root), "step": "dry-run"}


def prepare(vault_root, *, deadline=None) -> dict:
    """Pre-import and prove every file Records and Planning collection into retained staging.

    It runs beside a live writer: it reads vault files and copies the live store with the
    backup API, takes no lease and writes only its own staging under the state directory.
    A later ``cutover`` carries it forward when nothing it read has changed. A managed
    standby passes ``deadline`` (absolute monotonic) to settle inside its warm budget.
    """
    admission.require_owner(vault_root, "the collections-store migration")
    capability.require_collections_migrate()
    root = Path(vault_root).resolve()
    coordinator = preflight.coordinator_capability()
    if coordinator["status"] == "not_configured":
        raise admission.coordination_required("the collections-store migration")
    if _require_stageable(root) is not None:
        raise CollectionStoreError("COLLECTION_STORE_LEASE_REQUIRED",
                                   "a migration cutover is unfinished; run the apply step to finish it")
    with _migration_scope(root, deadline):
        if _require_stageable(root) is not None:
            raise CollectionStoreError("COLLECTION_STORE_LEASE_REQUIRED", "a migration cutover is unfinished")
        old = _staging_directories(root)
    report, directory = _stage(root, deadline=deadline)
    if directory is not None:
        with _migration_scope(root, deadline):
            if _require_stageable(root) is None:
                for stale in old:
                    _discard(stale)
    return {**report, "step": "prepare", "coordinator": coordinator,
            "staging": "retained" if directory is not None else "none"}


def discard_staging(vault_root) -> int:
    """Remove retained staging that no unfinished cutover holds; how many directories went.

    A managed standby calls it when its handoff cannot run the collections step. Staging
    beside a pending cutover stays, for that cutover's apply step to judge.
    """
    root = Path(vault_root).resolve()
    with _migration_scope(root):
        if _require_stageable(root) is not None:
            return 0
        return sum(_discard(directory) for directory in _staging_directories(root))


def _require_current_coordinator(runtime, fence_client, marker, staging, supersedes=None):
    """Refuse before any live change unless the coordinator's fence and head fit the staging store.

    Without a marker the fence must be unenrolled (never, or retired by an export), so the
    first cut succeeds at the next generation, or still bind the store ``supersedes`` names
    at its generation, so the supersede CAS succeeds; with a marker the
    fence must bind its store, the store must record no divergence, and the head the
    coordinator recorded must be in the staging chain from this instance. This prevents installing a store whose admission can
    never finish; a false refusal costs the owner a retry once the vault replica arrives.
    Returns the fence it read, the basis of a first enrolment's target marker.
    """
    fence = fence_client.collection_store_fence()
    if marker is None and supersedes is not None:
        if not fence.enrolled or {"store_id": fence.store_id, "generation": fence.generation} != supersedes:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT",
                                       "the coordinator's store fence moved during the migration")
        return fence
    if marker is None:
        if fence.enrolled:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT",
                                       "the coordinator already fences a store for this vault, which has no marker")
        return fence
    admission._require_fence(fence, marker)
    meta = legacy_originals.metadata(staging)
    if schema.META_REPLICA_DIVERGENCE in meta:
        raise CollectionStoreError(takeover.DIVERGED, "this store records unresolved divergence; "
                                   "run adopt-local, then migrate")
    recorded = admission._acquired(runtime, fence, "COLLECTION_STORE_LEASE_REQUIRED").collection_store_head
    if recorded is None:
        return fence
    if (recorded.store_id != meta[schema.META_STORE_ID] or recorded.instance_id != meta[schema.META_INSTANCE_ID]
            or chain.head_relation(staging, recorded.commit_seq, recorded.head_hash) not in ("same", "ancestor")):
        raise CollectionStoreError(takeover.SYNC_PENDING, "the coordinator's recorded head is not in this store; "
                                   "wait for the vault replica, open the store, then migrate again")
    return fence


def _intent(root, marker, raw, staging, fence, supersedes=None):
    """The migration intent and target marker for a verified staging store, against ``fence``.

    Over a live store it records that store's head as ``base_head``, the base the imports
    extend; a supersede records the store and generation the fence moves from.
    """
    sid = staging.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    basis = _read_basis(staging)
    recorded = basis["collections"]
    entries = [authority.marker_entry(cid, entry["manifest_path"], sid, entry["semantic_profile"],
                                      entry["source_path"], entry["layout"])
               for cid, entry in sorted(recorded.items(), key=lambda item: item[1]["manifest_path"])]
    target = admission.target_marker(root, marker, sid, entries, fence=fence, supersede=supersedes is not None)
    for entry in entries:
        # Prevents a migrated collection from taking a file collection's namespace; a false
        # refusal leaves the vault in files until the overlap is repaired.
        authority.require_new_file_ownership(root, target, entry)
    return {"version": 2, "kind": authority.MIGRATE, "request_id": "migrate:" + uuid.uuid4().hex,
            "collections": [{"collection_id": entry["collection_id"], "manifest_path": entry["manifest_path"],
                             "source_path": entry["source_path"], "txn_id": recorded[entry["collection_id"]]["txn_id"],
                             "basis": recorded[entry["collection_id"]]["basis"]}
                            for entry in entries],
            "expected_marker": None if raw is None else raw.decode("utf-8"),
            "target_marker": json.dumps(target, sort_keys=True, separators=(",", ":")),
            **({} if supersedes is None else {"supersedes": supersedes}),
            **({} if basis["live"] is None else {"base_head": {name: basis["live"][name]
                                                               for name in ("commit_seq", "head_hash")}})}


def _seal(directory, intent, adopted=None, *, expected_identity, expected_version, basis_text, check, deadline=None):
    """Record the intent in the staging store and close it journal-free, ready to rename into place.

    It opens only the existing file and replaces only the basis this cutover verified, so a
    staging store removed or recreated meanwhile (by a concurrent prepare) refuses rather
    than publishing an empty or unverified store. ``adopted`` is the digest of a vault
    replica that the store being superseded published: the first publication keeps those
    bytes as ``.foreign-*`` evidence instead of reading them as divergence.
    """
    path = directory / _STAGING
    check()
    _check_deadline(deadline)
    try:
        with _held_staging(directory) as (_filesystem, _parent, database, named_check):
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, isolation_level=None, timeout=0)) as staging:
                if not _single_file(staging, directory):
                    raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store requires SQLite companions")
                # Private staging keeps SQLite's writer lock through version capture; contention costs one safe retry.
                if staging.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone() != ("exclusive",):
                    raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store cannot retain its writer lock")
                # This coordinated attempt is the sole private writer; a busy writer costs one safe retry.
                staging.execute("BEGIN IMMEDIATE")
                check()
                _check_deadline(deadline)
                named_check()
                if not held_fs._same_file_identity(database.identity, expected_identity):
                    raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging identity changed during cutover")
                if database.version() != expected_version or not _single_file(staging, directory):
                    raise CollectionStoreError("COLLECTION_STORE_BUSY", "the proved staging bytes changed during cutover")
                if _basis_text(staging) != basis_text:
                    raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store changed during cutover")
                deleted = staging.execute("DELETE FROM store_meta WHERE key=? AND value=?", (_BASIS, basis_text)).rowcount
                if deleted != 1:
                    raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging basis changed during cutover")
                if authority.pending_create(staging) is not None:
                    raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store already has an intent")
                staging.execute("INSERT INTO store_meta(key,value) VALUES (?,?)",
                                (authority.PENDING_CREATE, json.dumps(intent, sort_keys=True, separators=(",", ":"))))
                if adopted is not None:
                    staging.execute("INSERT OR REPLACE INTO store_meta(key,value) VALUES (?,?)",
                                    (schema.META_ADOPTED_FOREIGN_REPLICA, adopted))
                _check_deadline(deadline)
                staging.execute("COMMIT")
                chain.verify_store_chain(staging)
                if staging.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                    raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store is still open")
                # EXCLUSIVE may retain its own rollback journal until close; no main writes follow this witness.
                sealed_version = database.version()
            check()
            if database.version() != sealed_version or any(
                    (directory / (_STAGING + suffix)).exists() for suffix in snapshot._COMPANIONS):
                raise CollectionStoreError("COLLECTION_STORE_BUSY", "the sealed staging bytes changed after close")
            return sealed_version
    except sqlite3.OperationalError as error:
        raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store changed during cutover") from error


STALE = "COLLECTION_STORE_MIGRATION_STALE"
#: An apply that put back the store a migration superseded and continued it as a new tenure;
#: the route then migrates afresh under a new lease acquisition.
RESTORED = "restored"
#: The reason a restore records with the fork it makes, where adopt-local records the owner's.
_RESTORE_WHY = "the files of a migration changed after it published its store, so the store it superseded continues"


def _changed_since_publication(session, fence_client, intent, deadline=None):
    """The paths of the intent's collections whose files no longer match the basis they were imported from.

    A collection not checked by ``deadline`` counts as changed.
    """
    root = session.root
    with preflight.scanned(root, _scratch(root), [], deadline=deadline) as (audit, _blocked), ExitStack() as inputs:
        if audit is None:
            return sorted(entry["manifest_path"] for entry in intent["collections"])
        opened, _ = connection._open_read_only(session.path, connection.BUSY_TIMEOUT_MS)
        reader = inputs.enter_context(closing(opened))
        reader.execute("BEGIN")
        proof = (inputs.enter_context(_supersede_ownership(session, fence_client, intent, reader=reader))
                 if intent["expected_marker"] is None and "base_head" not in intent
                 else ownership.resolve(root, conn=reader, manager=session.manager))
        presentation = inputs.enter_context(legacy_import.presentation_context(root, conn=reader, ownership_proof=proof))
        changed = []
        for entry in intent["collections"]:
            try:
                captured = legacy_import.capture_legacy_collection(
                    root, entry["manifest_path"], audit=audit, presentation_operation=presentation
                )
            except (TimeoutError, *preflight._REFUSALS):
                changed.append(entry["manifest_path"])
                continue
            if (captured.manifest.collection_id, captured.collection_basis) != (entry["collection_id"], entry["basis"]):
                changed.append(entry["manifest_path"])
        return sorted(changed)


def _contains(conn, commit_seq, head_hash):
    """Whether the store ``conn`` reads holds this head; every store holds the genesis head."""
    # nosemgrep: ep-word-membership -- chain.head_relation returns these closed relation tokens.
    return commit_seq == 0 or chain.head_relation(conn, commit_seq, head_hash) in ("same", "ancestor")


def _replica_digest(root):
    """The vault replica's digest, or None when there is none."""
    try:
        with open(replica.replica_path(root), "rb") as file:
            return hashlib.file_digest(file, "sha256").hexdigest()
    except FileNotFoundError:
        return None


def _adopted_replica(root, path):
    """The vault replica's digest when the live store ``path`` published it or already adopted it, else None.

    Fresh staging that supersedes that store names these bytes for its first publication
    to keep as evidence.
    """
    digest = _replica_digest(root)
    with closing(connection.open_reader(path)) as live:
        known = dict(live.execute("SELECT key,value FROM store_meta WHERE key IN (?,?)", (
            schema.META_LAST_PUBLISHED_REPLICA_SHA256, schema.META_ADOPTED_FOREIGN_REPLICA)))
    return digest if digest is not None and digest in known.values() else None


def _supersedes(root, fence, pending):
    """The store and generation a first enrolment's cut fence binds, which fresh staging supersedes (P1b.3c).

    No marker routes to this migration's own unadmitted store, the pending intent's target
    or the store that intent itself superseded, so replacing it loses nothing. A fence that
    binds any other store refuses: moving it would take an enrolment that another step
    made. A false refusal leaves the owner the manual repair of restoring the files.
    """
    published = authority.parse_marker(root, pending["target_marker"])["store_id"]
    if fence.store_id not in {published, (pending.get("supersedes") or {}).get("store_id")}:
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT",
                                   "the coordinator fences a store that this migration did not publish")
    return {"store_id": fence.store_id, "generation": fence.generation}


def _newest_superseded(session):
    """The one superseded store ``takeover.replace_live`` keeps beside the live store, or None."""
    found = sorted(session.path.parent.glob(session.path.name + takeover.SUPERSEDED + "*"))
    return found[0] if len(found) == 1 else None


def _superseded_migration(session, raw):
    """Whether the newest superseded store is a migration that a restore stopped part way past (P1b.3c).

    The superseded store then carries a migration whose expected marker is the current
    marker, and the live store carries no intent and still ends at the head that
    migration imported onto. A restored store that committed since was admitted, which
    a restore short of its fork cannot be (takeover refuses the migration's replica), so
    nothing is left to finish. A store this release cannot read is not that evidence.
    """
    superseded = _newest_superseded(session)
    if superseded is None:
        return False
    try:
        with closing(connection.open_reader(superseded)) as reader:
            intent = authority.pending_create(reader)
        with closing(connection.open_reader(session.path)) as live:
            head = chain.recorded_head(live)
    except (CollectionStoreError, sqlite3.DatabaseError):
        return False
    return (intent is not None and intent["kind"] == authority.MIGRATE
            and intent["expected_marker"] is not None and intent["expected_marker"].encode() == raw
            and "base_head" in intent and head == (intent["base_head"]["commit_seq"], intent["base_head"]["head_hash"]))


def _imports_onto(base, migrated, intent):
    """Whether ``migrated`` is ``base`` plus only ``intent``'s legacy imports, which a restore may drop.

    ``base`` must end at the head the intent recorded as ``base_head``, ``migrated`` must
    hold that head, and every later transaction must import one of the intent's collections.
    """
    recorded = intent.get("base_head")
    if recorded is None:
        return False
    try:
        head = chain.verify_store_chain(base)
        chain.verify_store_chain(migrated)
    except chain.StoreChainError:
        return False
    identity = "SELECT value FROM store_meta WHERE key='store_id'"
    if (head != (recorded["commit_seq"], recorded["head_hash"]) or not _contains(migrated, *head)
            or base.execute(identity).fetchone() != migrated.execute(identity).fetchone()):
        return False
    ids = sorted(authority.intent_collection_ids(intent))
    return migrated.execute(
        "SELECT 1 FROM txns WHERE commit_seq>? AND (operation!='legacy_import' "
        f"OR collection_id NOT IN ({','.join('?' * len(ids))})) LIMIT 1", (head[0], *ids)).fetchone() is None


def _restore(session, manager, fence_client, raw, changed=()):
    """Put back the store a migration over an enrolled vault superseded, as this host's tenure (P1b.3c).

    The live store and the newest superseded store are the migration's store and the
    store it superseded, either way round: before the restore the migration's is live,
    after it the superseded one. The migration's store must be the other plus only its
    intent's imports. When its publication or head reached the vault replica or the
    coordinator, the restored store continues past them as a new tenure, as adopt-local
    continues one, and keeps that replica as ``.foreign-*`` evidence. It then publishes
    and records its head. Returns the restored head, and whether it restored and forked.
    A refusal names the ``changed`` manifests the caller found.
    """
    root = session.root
    marker = authority.parse_marker(root, raw)
    runtime = session.runtime()
    with manager.writer_authority_guard(vault_root=root):
        token = manager._fencing_token
        fence = admission._require_fence(fence_client.collection_store_fence(), marker)
        recorded = admission._acquired(runtime, fence, "COLLECTION_STORE_LEASE_REQUIRED").collection_store_head
        superseded = _newest_superseded(session)
        if superseded is None:
            raise _unproven(changed)
        with closing(connection.open_reader(session.path)) as live, \
                closing(connection.open_reader(superseded)) as other:
            restoring = authority.pending_create(live)
            migrated, base = (live, other) if restoring is not None else (other, live)
            intent = restoring if restoring is not None else authority.pending_create(other)
            if (intent is None or intent["kind"] != authority.MIGRATE or intent["expected_marker"] is None
                    or intent["expected_marker"].encode() != raw or not _imports_onto(base, migrated, intent)):
                # Prevents dropping a write the restore cannot prove is only this migration's
                # import; a false refusal leaves the owner the manual repair of the files.
                raise _unproven(changed)
            meta = legacy_originals.metadata(base)
            if schema.META_REPLICA_DIVERGENCE in meta or schema.META_VIEW_DIVERGED in meta:
                raise CollectionStoreError(takeover.DIVERGED, "the store this migration superseded records "
                                           "unresolved divergence; run adopt-local, then migrate")
            lineage = {entry["instance_id"] for entry in json.loads(meta[schema.META_LINEAGE])}
            (published,) = migrated.execute("SELECT value FROM store_meta WHERE key=?", (
                schema.META_LAST_PUBLISHED_REPLICA_SHA256,)).fetchone() or (None,)
            known = {published, meta.get(schema.META_LAST_PUBLISHED_REPLICA_SHA256),
                     meta.get(schema.META_ADOPTED_FOREIGN_REPLICA)} - {None}
            if (recorded is not None and (recorded.instance_id not in lineage or not any(
                    _contains(conn, recorded.commit_seq, recorded.head_hash) for conn in (base, migrated)))
                    or _replica_digest(root) not in known):
                # Prevents continuing past a head or replica that another host wrote, whose rows
                # the restored store lacks; a false refusal costs a retry once the replica arrives.
                raise CollectionStoreError(takeover.SYNC_PENDING, (
                    "the coordinator's recorded head or the vault replica is neither the store this migration "
                    "superseded nor its import; wait for the vault replica, then migrate again"))
        if restoring is not None:
            if not runtime.retire_idle_handle():
                raise CollectionStoreError(takeover.BUSY, "the live store is still borrowed")
            session.require(token)
            takeover.replace_live(session, superseded.name, lambda: session.require(token))
        sealed = owner.adopt_local_preview(root, recorded=recorded)
        forked = sealed["preview"]["state"] != "in_sync"
        if forked:
            publication = admission.continue_past(session, runtime, token, sealed["preview"], why=_RESTORE_WHY)[0]
        else:
            with session.writer(token) as writer:
                publication = admission._publish_current_epoch(session, writer, token)
        if publication.status != "published":
            raise CollectionStoreError("COLLECTION_STORE_FLUSH_PENDING",
                                       f"replica publication: {publication.reason or publication.status}")
        manager._renew_collection_store(token)
    return {"store_id": marker["store_id"], "restored": restoring is not None, "forked": forked,
            "head": {"commit_seq": publication.commit_seq, "head_hash": publication.head_hash}}


def _unproven(changed=()):
    named = f" ({', '.join(changed)})" if changed else ""
    return CollectionStoreError(STALE, (
        f"collections changed after the migration published its store{named}, and that store cannot be proven to "
        "be the store it superseded plus only its imports, so nothing was restored. Restore their files to the "
        "bytes the migration imported (from a backup or the sync history), then run "
        "`exomem collections migrate --apply` again"))


def _fenced(session, fence_client):
    """Whether a lease on this vault needs the store capability.

    While an intent is pending, only a process with a bound store runtime sends it.
    """
    return authority.read_marker(session.root) is not None or fence_client.collection_store_fence().enrolled


def _hold_lease(session, manager, root, *, fenced):
    """Take the writer lease and return its token; it stays held after the authority check."""
    if fenced:
        session.runtime()
    with manager.writer_authority_guard(vault_root=root):
        return manager._fencing_token


#: How a managed handoff's migration ends without publishing (design §10, D2).
ABANDONED = handoff_state.ABANDONED
#: Why it was abandoned when no carry-forward reason above says so: its deadline passed,
#: or a step refused or failed before anything was published.
DEADLINE, REFUSED = "deadline", "refused"


def _abandoned(coordinator, reason, changed=()):
    """The report of a handoff's migration that publishes nothing; the next handoff retries it."""
    return {"mode": "collections-store", "step": "apply", "coordinator": coordinator, "status": ABANDONED,
            "abandoned_because": reason, **({"changed": list(changed)} if changed else {})}


@contextmanager
def _supersede_ownership(session, fence_client, pending, *, reader=None):
    """Prove that the first import's entire namespace is its own pending file input."""
    with ExitStack() as inputs:
        if reader is None:
            opened, _ = connection._open_read_only(session.path, connection.BUSY_TIMEOUT_MS)
            reader = inputs.enter_context(closing(opened))
            reader.execute("BEGIN")
        intent = authority.pending_create(reader)
        if (intent != pending or intent is None or intent["kind"] != authority.MIGRATE
                or intent["expected_marker"] is not None or "base_head" in intent):
            raise CollectionStoreError(takeover.BUSY, "the first import changed before its replacement")
        authority.require_intent_binding(reader, intent)
        canonical, declared = ownership.declarations(session.root, reader)
        participants = authority.intent_collection_ids(intent)
        # Replacing an unproved base could lose stored rows; a refusal costs recovery or a retry.
        if canonical is None or {entry["collection_id"] for entry in canonical["collections"]} != participants:
            raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "the pending import does not own the whole store")
        target = authority.parse_marker(session.root, intent["target_marker"])
        if {entry["collection_id"] for entry in target["collections"]} != participants:
            raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "the import target differs from its cohort")
        for entry in target["collections"]:
            authority.require_selected(reader, target, entry, root=session.root)
        status = authority.intent_status(session.root, reader, intent)
        if status != "pending":
            raise CollectionStoreError(takeover.BUSY, "the import's marker preimage changed")
        fence = fence_client.collection_store_fence()
        if not authority.first_import_fence_matches(intent, target, fence):
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "the import's collection fence changed")
        sid, instance = (reader.execute("SELECT value FROM store_meta WHERE key=?", (key,)).fetchone()[0]
                         for key in (schema.META_STORE_ID, schema.META_INSTANCE_ID))
        binding = (str(session.path), sid, instance, declared, json.dumps(intent, sort_keys=True), status,
                   (fence.capability, fence.enrolled, fence.store_id, fence.generation))
        yield ownership.Proof(binding, None)


def cutover(session, manager, fence_client, *, deadline=None) -> dict:
    """Make verified staging the live store and admit it, inside the owner's offline session.

    A live store carrying this migration's intent (a cutover that stopped after it
    published the store) resumes without importing again, once every collection still
    matches the basis it was imported from. When one changed, the unadmitted store is
    superseded (P1b.3c): on a first enrolment fresh staging replaces it and moves its
    fence, and on an enrolled vault the store it superseded is restored first. Otherwise,
    under the mutation boundary, staging is carried forward or rebuilt; a blocked
    collection leaves no store, no marker and an unenrolled fence, and the report names
    it with its failed check.

    A managed handoff passes ``deadline`` (absolute monotonic), its self-imposed budget
    (D3). It rebuilds staging only within that budget and abandons, publishing nothing,
    when the budget runs out first or a live S1 store's head moved since the standby
    copied it (D4); a cutover that published its store always finishes.
    """
    capability.require_collections_migrate()
    root = session.root
    session.verify_custody()
    admission._ProducerSession.bind(session, manager)
    admission._bind_fence_client(session, fence_client)
    coordinator = preflight.coordinator_capability(fence_client)
    with manager.consistency_guard(root, operation="collection_store_migrate"):
        pending = _require_stageable(root)
        changed, supersedes, restored = [], None, None
        if pending is not None:
            with closing(connection.open_head_reader(session.path)) as reader:
                status = authority.intent_status(root, reader, pending)
            # Until its marker is installed the files stay canonical, so every collection is
            # compared with the basis it was imported from: first without the lease, so a
            # refusal holds none, then again under it before any fence step or marker.
            changed = (_changed_since_publication(session, fence_client, pending, deadline)
                       if status == "pending" else [])
            if not changed:
                token = _hold_lease(session, manager, root, fenced=_fenced(session, fence_client))
                changed = (_changed_since_publication(session, fence_client, pending, deadline)
                           if status == "pending" else [])
                if not changed:
                    result = admission._resume_locked(session, fence_client, preparation_token=token)
                    with _migration_scope(root):
                        for leftover in _staging_directories(root):
                            _discard(leftover)
                    return {"mode": "collections-store", "step": "apply", "coordinator": coordinator,
                            "staging": "resumed", **result}
            if _expired(deadline):
                # A comparison the budget cut short proves no change: the store stays pending for
                # the next handoff or the owner's apply, which compare again.
                return _abandoned(coordinator, DEADLINE, changed)
            # Once a fence binds the published store, writers inside the writer lease are fenced
            # from these files, so only a writer outside it changed them. Admitting would lose
            # that change, so the unadmitted store gives way to fresh staging that imports it.
            if pending["expected_marker"] is not None:
                # The marker routes the store this migration superseded: restore it first.
                restored = {**_restore(session, manager, fence_client, pending["expected_marker"].encode(), changed),
                            "changed": changed}
                pending = None
            else:
                fence = fence_client.collection_store_fence()
                if fence.enrolled:
                    supersedes = _supersedes(root, fence, pending)
        raw = authority.read_marker(root)
        marker = None if raw is None else authority.parse_marker(root, raw)
        if pending is None and restored is None and raw is not None and _superseded_migration(session, raw):
            # A restore that stopped before it continued the restored store as this host's tenure.
            restored = _restore(session, manager, fence_client, raw)
        if restored is not None and restored["forked"]:
            # The fork moved the coordinator's head past this lease's acquisition, which the
            # cutover's checks read; the route migrates again under a new acquisition.
            return {"mode": "collections-store", "step": "apply", "coordinator": coordinator,
                    "status": RESTORED, "restored": restored}
        with ExitStack() as inputs:
            supersede = pending is not None
            # Staging runs under the mutation boundary alone, which keeps the files and the live
            # store still only for processes that share this lease state directory. The lease waits
            # until there is a store to publish, because an unadmitted token cannot hand it back.
            with _migration_scope(root, deadline):
                current_pending = _require_stageable(root)
                if current_pending != pending:
                    raise CollectionStoreError("COLLECTION_STORE_BUSY", "the live migration changed during coordination")
                ownership_proof = inputs.enter_context(_supersede_ownership(session, fence_client, pending)) if supersede else None
                directories = _staging_directories(root)
            if supersede:
                # Nothing fences or routes the published store yet: fresh staging supersedes it.
                report, reason, directory = None, BASIS_CHANGED, None
            else:
                report, reason, changed = _carried(root, directories[0], deadline=deadline,
                                                   ownership_proof=ownership_proof) if len(directories) == 1 else (
                    None, NO_STAGING if not directories else SEVERAL_STAGINGS, [])
                directory = directories[0] if len(directories) == 1 else None
            if report is None:
                if deadline is not None and reason == LIVE_CHANGED:
                    # Copying the moved store again is the owner's offline apply (D4).
                    return _abandoned(coordinator, reason)
                report, directory = _stage(root, base=not supersede, deadline=deadline,
                                           ownership_proof=ownership_proof, donor=directory if not supersede and reason not in (
                                               INTERRUPTED, STAGING_MISSING, NO_BASIS) else None)
            if directory is None:
                if _expired(deadline):
                    return _abandoned(coordinator, DEADLINE, changed)
                return {**report, "step": "apply", "coordinator": coordinator, "staging": "none"}
            # A cut fence admits only a lease that carries the store capability, which a bound runtime sends.
            runtime = session.runtime() if marker is not None or supersedes is not None else None
            with manager.writer_authority_guard(vault_root=root):
                token = manager._fencing_token
                # Staging held no lease, so a lease holder with another state directory may have
                # written meanwhile. Under the lease none can; only a writer outside it still can.
                with ExitStack() as final:
                    with _migration_scope(root, deadline):
                        _filesystem, _parent, staged_file, staging_check = final.enter_context(_held_staging(directory))
                        proved_version = _proved_version(directory, staged_file)
                    verified = {}
                    rechecked, why, rechanged = _carried(root, directory, supersede=supersede, deadline=deadline,
                                                       verified=verified, ownership_proof=ownership_proof)
                    if rechecked is None:
                        final.close()
                        if deadline is not None and why == LIVE_CHANGED:
                            return _abandoned(coordinator, why)
                        report, directory = _stage(root, base=not supersede, deadline=deadline,
                                                   ownership_proof=ownership_proof, donor=None if why == NO_BASIS else directory)
                        reason, changed = why, rechanged
                        if directory is None:
                            if _expired(deadline):
                                return _abandoned(coordinator, DEADLINE, changed)
                            return {**report, "step": "apply", "coordinator": coordinator, "staging": "none"}
                        with _migration_scope(root, deadline):
                            _filesystem, _parent, staged_file, staging_check = final.enter_context(_held_staging(directory))
                            proved_version = _proved_version(directory, staged_file)
                        rechecked, why, rechanged = _carried(root, directory, supersede=supersede, deadline=deadline,
                                                           verified=verified, ownership_proof=ownership_proof)
                        if rechecked is None:
                            if deadline is not None:
                                return _abandoned(coordinator, DEADLINE if _expired(deadline) else why, rechanged)
                            raise CollectionStoreError("COLLECTION_STORE_BUSY", "the migration inputs changed at the final comparison")
                    with _migration_scope(root, deadline):
                        staging_check()
                        if staged_file.version() != proved_version:
                            raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging bytes changed during the final proof")
                        with closing(connection.open_reader(directory / _STAGING, busy_timeout_ms=0)) as staging:
                            fence = _require_current_coordinator(runtime, fence_client, marker, staging, supersedes)
                            intent = _intent(root, marker, raw, staging, fence, supersedes)
                            basis_text = verified["basis_text"]
                            if _basis_text(staging) != basis_text:
                                raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging basis changed after comparison")
                            identity = tuple(staging.execute("SELECT value FROM store_meta WHERE key=?", (key,)).fetchone()[0]
                                             for key in (schema.META_STORE_ID, schema.META_INSTANCE_ID))
                        if _expired(deadline):
                            # The last point before a live change: from the seal on, the cutover finishes.
                            return _abandoned(coordinator, DEADLINE, changed)
                        if ownership_proof is not None:
                            with _supersede_ownership(session, fence_client, pending) as current:
                                if current.binding != ownership_proof.binding:
                                    raise CollectionStoreError(takeover.BUSY, "the import source changed before replacement")
                        inputs.close()  # No source reader may survive installation of its replacement.
                        sealed_version = _seal(directory, intent, _adopted_replica(root, session.path) if supersede else None,
                                               expected_identity=staged_file.identity, expected_version=proved_version,
                                               basis_text=basis_text, check=staging_check, deadline=deadline)
                        if runtime is not None and not runtime.retire_idle_handle():
                            raise CollectionStoreError(takeover.BUSY, "the live store is still borrowed")
                        session.require(token)
                        takeover.replace_live(session, _STAGING, lambda: session.require(token),
                                              subdirectory=directory.name, expected_identity=staged_file.identity,
                                              expected_version=sealed_version)
                        if supersedes is not None:
                            # The runtime opened the superseded store; it now serves the store the fence moves to.
                            runtime.rebind(identity)
        result = admission._resume_locked(session, fence_client, preparation_token=token)
        with _migration_scope(root):
            for leftover in _staging_directories(root):
                _discard(leftover)
    return {**report, "step": "apply", "coordinator": coordinator,
            "staging": "carried_forward" if reason is None else "rebuilt",
            **({"rebuilt_because": reason, "changed": changed} if reason is not None else {}),
            **({"superseded": supersedes} if supersedes is not None else {}),
            **({"restored": restored} if restored is not None else {}), **result}


#: What a managed handoff's collections step ended in: ``handoff_state``'s closed outcomes,
#: beside ``ABANDONED``, ``RESTORED`` and the report verdicts.
MIGRATED, PENDING, UNFINISHED, UNSAFE = (handoff_state.MIGRATED, handoff_state.PENDING,
                                         handoff_state.UNFINISHED, handoff_state.UNSAFE)


def settle_handoff(vault_root, *, deadline) -> dict:
    """The collections step of a managed handoff's migrator (design §10, D2 and D3).

    It runs the owner's apply, with the operator credential that the supervisor gave this
    child alone, under the self-imposed ``deadline`` (absolute monotonic). A migration it
    cannot finish in time, or whose live S1 store moved, is abandoned before anything is
    published, so the release completes in file mode and the next handoff retries it. A
    refusal or a fault settles by what it left durable, never by where it raised: before
    publication it is abandoned; after admission the store is migrated; a published store
    that still carries its intent is ``pending``: the replacement serves the vault, read-only
    behind a cut fence or in file mode before the cut, until the next handoff or the owner's
    apply finishes it, rechecking every file first.
    ``unsafe`` is the one outcome that fails the handoff.
    """
    root = Path(vault_root).resolve()
    before = handoff_state.left(root)
    try:
        result = admission.migrate_route(root, step="handoff", deadline=deadline)
    except Exception as error:  # noqa: BLE001 - classified by the durable state it left, below
        logger.warning("the managed collections migration raised", exc_info=True)
        code = getattr(error, "code", None) or type(error).__name__
        published = handoff_state.published(before, handoff_state.left(root))
        if published is None:
            # A deadline a copy or scan ran into is this handoff's budget, as in `cutover`.
            result = _abandoned(None, DEADLINE if isinstance(error, TimeoutError) else REFUSED)
            return {**result, "code": code, "outcome": ABANDONED}
        return {"mode": "collections-store", "step": "apply", "status": "raised", "code": code,
                "outcome": published}
    published = handoff_state.published(before, handoff_state.left(root))
    if result.get("status") == "marker_admitted":
        outcome = MIGRATED
    elif published == UNSAFE:
        outcome = UNSAFE
    elif published == PENDING:
        outcome = PENDING
    elif result.get("status") in (ABANDONED, RESTORED):
        outcome = result["status"]
    # nosemgrep: ep-word-membership -- preflight.report's closed verdicts that publish nothing.
    elif result.get("verdict") in ("blocked", "nothing_to_migrate"):
        # A blocked or empty report published nothing either; its verdict says which.
        outcome = result["verdict"]
    else:
        outcome = UNFINISHED
    return {**result, "outcome": outcome}
