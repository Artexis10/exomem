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
    owner,
    preflight,
    replica,
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


#: The fields ``_recorder`` writes per collection; a basis missing one is rebuilt, never read.
_RECORDED = frozenset({"manifest_path", "source_path", "layout", "semantic_profile", "basis", "txn_id", "rows",
                       "legacy_events", "legacy_audit_status"})


def _usable(basis):
    try:
        return (basis["version"] == 1
                and (basis["live"] is None or isinstance(basis["live"]["snapshot_sha256"], str))
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


def _stage(root, *, base=True):
    """Build fresh staging from the live store (if any) and every file collection's import and proof.

    ``base=False`` starts from an empty store, for a first enrolment whose own unadmitted
    store is live. Returns the report and the retained staging directory, or None when
    nothing is ready or a collection is blocked: a failed proof leaves no staging behind.
    """
    for old in _staging_directories(root):
        _discard(old)
    rows, pending, store_ids = preflight.candidates(root)
    directory = _new_directory(root)
    try:
        live = _live_snapshot(root, directory) if base and connection.store_path(root).exists() else None
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


def _carried(root, directory, *, supersede=False):
    """The report for staging that still matches every input it was built from, or why it does not.

    Each collection is compared by its own basis, so an unrelated activity-log write does
    not invalidate it; the live store, when one exists, must be unchanged since its copy.
    ``supersede`` ignores a live store that is this migration's own unadmitted one.
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
    if basis is None or not _usable(basis):
        return None, NO_BASIS, []
    live = connection.store_path(root).exists() and not supersede
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
    meta = dict(staging.execute("SELECT key,value FROM store_meta"))
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


def _seal(directory, intent, adopted=None):
    """Record the intent in the staging store and close it journal-free, ready to rename into place.

    It opens only the existing file and replaces only the basis this cutover verified, so a
    staging store removed or recreated meanwhile (by a concurrent prepare) refuses rather
    than publishing an empty or unverified store. ``adopted`` is the digest of a vault
    replica that the store being superseded published: the first publication keeps those
    bytes as ``.foreign-*`` evidence instead of reading them as divergence.
    """
    path = directory / _STAGING
    try:
        with closing(sqlite3.connect(f"{path.as_uri()}?mode=rw", uri=True, isolation_level=None)) as staging:
            staging.execute("BEGIN IMMEDIATE")
            if _read_basis(staging) is None:
                raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store changed during cutover")
            staging.execute("DELETE FROM store_meta WHERE key=?", (_BASIS,))
            staging.execute("INSERT INTO store_meta(key,value) VALUES (?,?)",
                            (authority.PENDING_CREATE, json.dumps(intent, sort_keys=True, separators=(",", ":"))))
            if adopted is not None:
                staging.execute("INSERT OR REPLACE INTO store_meta(key,value) VALUES (?,?)",
                                (schema.META_ADOPTED_FOREIGN_REPLICA, adopted))
            staging.execute("COMMIT")
            chain.verify_store_chain(staging)
            if staging.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store is still open")
    except sqlite3.OperationalError as error:
        raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store changed during cutover") from error
    if any((directory / (_STAGING + suffix)).exists() for suffix in snapshot._COMPANIONS):
        raise CollectionStoreError("COLLECTION_STORE_BUSY", "the staging store is still open")


STALE = "COLLECTION_STORE_MIGRATION_STALE"
#: An apply that put back the store a migration superseded and continued it as a new tenure;
#: the route then migrates afresh under a new lease acquisition.
RESTORED = "restored"
#: The reason a restore records with the fork it makes, where adopt-local records the owner's.
_RESTORE_WHY = "the files of a migration changed after it published its store, so the store it superseded continues"


def _changed_since_publication(root, intent):
    """The paths of the intent's collections whose files no longer match the basis they were imported from."""
    with preflight.scanned(root, _scratch(root), []) as (audit, _blocked):
        if audit is None:
            return sorted(entry["manifest_path"] for entry in intent["collections"])
        changed = []
        for entry in intent["collections"]:
            try:
                captured = legacy_import.capture_legacy_collection(root, entry["manifest_path"], audit=audit)
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
            meta = dict(base.execute("SELECT key,value FROM store_meta"))
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
    """Whether a lease on this vault needs the store capability, which only a bound runtime sends."""
    return authority.read_marker(session.root) is not None or fence_client.collection_store_fence().enrolled


def _hold_lease(session, manager, root, *, fenced):
    """Take the writer lease and return its token; it stays held after the authority check."""
    if fenced:
        session.runtime()
    with manager.writer_authority_guard(vault_root=root):
        return manager._fencing_token


def cutover(session, manager, fence_client) -> dict:
    """Make verified staging the live store and admit it, inside the owner's offline session.

    A live store carrying this migration's intent (a cutover that stopped after it
    published the store) resumes without importing again, once every collection still
    matches the basis it was imported from. When one changed, the unadmitted store is
    superseded (P1b.3c): on a first enrolment fresh staging replaces it and moves its
    fence, and on an enrolled vault the store it superseded is restored first. Otherwise,
    under the mutation boundary, staging is carried forward or rebuilt; a blocked
    collection leaves no store, no marker and an unenrolled fence, and the report names
    it with its failed check.
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
                admitted = authority.intent_status(root, reader, pending) == "marker_admitted"
            # Until its marker is installed the files stay canonical, so every collection is
            # compared with the basis it was imported from: first without the lease, so a
            # refusal holds none, then again under it before any fence step or marker.
            changed = [] if admitted else _changed_since_publication(root, pending)
            if not changed:
                token = _hold_lease(session, manager, root, fenced=_fenced(session, fence_client))
                changed = [] if admitted else _changed_since_publication(root, pending)
                if not changed:
                    result = admission._resume_locked(session, fence_client, preparation_token=token)
                    for leftover in _staging_directories(root):
                        _discard(leftover)
                    return {"mode": "collections-store", "step": "apply", "coordinator": coordinator,
                            "staging": "resumed", **result}
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
        supersede = pending is not None
        # Staging runs under the mutation boundary alone, which keeps the files and the live
        # store still only for processes that share this lease state directory. The lease waits
        # until there is a store to publish, because an unadmitted token cannot hand it back.
        directories = _staging_directories(root)
        if supersede:
            # Nothing fences or routes the published store yet: fresh staging supersedes it.
            report, reason, directory = None, BASIS_CHANGED, None
        else:
            report, reason, changed = _carried(root, directories[0]) if len(directories) == 1 else (
                None, NO_STAGING if not directories else SEVERAL_STAGINGS, [])
            directory = directories[0] if report is not None else None
        if report is None:
            report, directory = _stage(root, base=not supersede)
        if directory is None:
            return {**report, "step": "apply", "coordinator": coordinator, "staging": "none"}
        # A cut fence admits only a lease that carries the store capability, which a bound runtime sends.
        runtime = session.runtime() if marker is not None or supersedes is not None else None
        with manager.writer_authority_guard(vault_root=root):
            token = manager._fencing_token
            # Staging held no lease, so a lease holder with another state directory may have
            # written meanwhile. Under the lease none can; only a writer outside it still can.
            rechecked, why, rechanged = _carried(root, directory, supersede=supersede)
            if rechecked is None:
                report, directory = _stage(root, base=not supersede)
                reason, changed = why, rechanged
                if directory is None:
                    return {**report, "step": "apply", "coordinator": coordinator, "staging": "none"}
            with closing(connection.open_reader(directory / _STAGING)) as staging:
                fence = _require_current_coordinator(runtime, fence_client, marker, staging, supersedes)
                intent = _intent(root, marker, raw, staging, fence, supersedes)
                identity = tuple(staging.execute("SELECT value FROM store_meta WHERE key=?", (key,)).fetchone()[0]
                                 for key in (schema.META_STORE_ID, schema.META_INSTANCE_ID))
            _seal(directory, intent, _adopted_replica(root, session.path) if supersede else None)
            if runtime is not None and not runtime.retire_idle_handle():
                raise CollectionStoreError(takeover.BUSY, "the live store is still borrowed")
            session.require(token)
            takeover.replace_live(session, _STAGING, lambda: session.require(token), subdirectory=directory.name)
            if supersedes is not None:
                # The runtime opened the superseded store; it now serves the store the fence moves to.
                runtime.rebind(identity)
        result = admission._resume_locked(session, fence_client, preparation_token=token)
        _discard(directory)
    return {**report, "step": "apply", "coordinator": coordinator,
            "staging": "carried_forward" if reason is None else "rebuilt",
            **({"rebuilt_because": reason, "changed": changed} if reason is not None else {}),
            **({"superseded": supersedes} if supersedes is not None else {}),
            **({"restored": restored} if restored is not None else {}), **result}
