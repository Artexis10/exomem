"""`exomem collections cutover`: every structured collection to SQLite-only storage (design §17, C3.1).

The preview reads the C0.1 inventory and names, per collection, the steps the apply takes and
every blocker with its fix. It changes nothing and returns the ``plan_id`` that the apply quotes.
The apply runs offline, with the service stopped, as the owner's migrate and export do:

1. It takes a backup snapshot of the store through ``owner.backup`` and verifies it. The derived
   rows' file beside the store is recorded by size and sha256, not copied: those rows rebuild from
   the store's import log and the sources the vault preserves, and copying a large file would hold
   the stop window. Then it measures every collection it will change: a store collection from the
   snapshot, a file collection from a private proof import of its files, which is the import the
   migration makes.
2. It migrates the file collections through the existing migration (``admission.migrate_route``).
3. It converts each markdown-items collection (``conversion``). A summary-mode one has no item
   files, so only its storage strategy changes. Each conversion's revise begins the query projection
   of the collection's declaration, and the apply publishes it before the next collection, as the
   service's backfill would. A Planning write then finds the items that name a parent by index
   (``references.neighbourhood``) from the first request.

After each step it checks parity against the measures of step 1: row count, key set, payload
hashes and audit event count. A conversion checks them inside its own transaction, so a failure
leaves that collection and every later one as they were. A run record under the vault's state
root names the phase reached and each collection done, so a re-run of the same ``plan_id`` after
a crash resumes there and repeats nothing. A resume measures each collection that no step has
changed yet again, from its own new snapshot, because what stopped the run may have needed a change
to one; the first backup stays the state before the run. A backup never replaces a file. The
finished record is the receipt.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

from .. import records, state_paths, writer_lease
from .. import structured_collections as collections
from ..maintenance_jobs import plan_identity, require_current_plan
from . import (
    admission,
    capability,
    connection,
    conversion,
    derived_rows,
    governance,
    index_migrations,
    inventory,
    ownership,
    preflight,
    types,
)
from . import summary as summary_module
from .connection import CollectionStoreError

MODE = "collections-cutover"
# The steps, phases, measures and codes below are this operation's closed protocol, not vocabulary.
MIGRATE, CONVERT, REWRITE = "migrate", "convert", "rewrite_strategy"
BACKED_UP, MIGRATED, COMPLETE = "backed_up", "migrated", "complete"
MEASURES = ("rows", "keys", "payload_hashes", "audit_events")
#: The fields of a conversion receipt that the run record keeps for each collection.
COUNTS = ("archive", "archived", "removed", "not_removed")
#: The states of a published or failed query projection mapping (``index_migrations``).
READY, FAILED = "ready", "failed"
#: The strategy the cutover cannot convert; a member of ``structured_collections.SUPPORTED_STORAGE``.
MARKDOWN_LOG = "markdown-log"
LOG_UNSUPPORTED = "COLLECTIONS_CUTOVER_LOG_UNSUPPORTED"
BLOCKED = "COLLECTIONS_CUTOVER_BLOCKED"
PARITY = "COLLECTIONS_CUTOVER_PARITY"
BACKUP_REQUIRED = "COLLECTIONS_CUTOVER_BACKUP_REQUIRED"
BACKUP_CHANGED = "COLLECTIONS_CUTOVER_BACKUP_CHANGED"
BACKUP_EXISTS = "COLLECTIONS_CUTOVER_BACKUP_EXISTS"
MIGRATION_INCOMPLETE = "COLLECTIONS_CUTOVER_MIGRATION_INCOMPLETE"
#: Why the backup records the derived rows' file and does not copy it.
DERIVED_NOT_COPIED = ("its rows rebuild from the store's import log and the sources the vault preserves, so the "
                      "backup names the file by size and sha256 and copies nothing")
#: The run records, beside the archives of the item files the conversions retired.
RUNS = "cutover"
APPLY = ("stop the service, then run `exomem collections cutover --plan-id ID --why REASON --backup-to FILE` "
         "on the host with EXOMEM_LEASE_COORDINATOR_OPERATOR_TOKEN set")
_SETTLE = ("start the service until edit-back applies or holds every edit to the collection's views and its "
           "views are published, stop it, then preview again")


def _blocker(code, reason, path=None):
    return {"path": path, "code": code, "reason": reason}


# Preview


def _inventory(root, file_rows, proof):
    """Every inventory row, page after page."""
    rows, after = [], None
    while True:
        found = inventory.page(root, file_rows, proof, after)
        rows.extend(found["collections"])
        if found["next_cursor"] is None:
            return rows
        after = inventory.position(found["next_cursor"])


@contextmanager
def _surveyed(root):
    """The ownership proof, the inventory rows and the private proof import of the file collections.

    The proof import is the preflight's: it captures each file collection and imports it into a
    private staging store, which lives as long as this context. It writes nothing the vault keeps.
    """
    proof = ownership.current(root)
    file_rows, pending, _ = preflight.candidates(root, ownership_proof=proof)
    with ExitStack() as stack:
        staging = None
        if pending:
            scratch = Path(stack.enter_context(tempfile.TemporaryDirectory(
                prefix="collections-preflight-", dir=state_paths.ensure_vault_state_dir(root))))
            staging = stack.enter_context(connection.staging_store(scratch / "staging.sqlite"))
            file_rows.extend(preflight.prove_into(root, pending, staging, scratch, preflight.import_context()))
        yield proof, _inventory(root, file_rows, proof), staging


def _steps(row):
    """The steps the apply takes on one inventory row, and the row's blocker."""
    strategy = row["storage_strategy"]
    if row["blocker"] is not None:
        return [], {**row["blocker"], "path": row["path"]}
    if strategy == MARKDOWN_LOG:
        return [], _blocker(LOG_UNSUPPORTED, "a markdown-log collection has no SQLite-only form in this release; "
                            "move its rows into a sqlite collection and remove this one, then preview again", row["path"])
    if row["authority"] == inventory.FILE:
        # A dataset keeps file authority; the preflight blocks every other file strategy it cannot migrate.
        return ([MIGRATE, CONVERT] if strategy == conversion.MARKDOWN_ITEMS else []), None
    if strategy == conversion.MARKDOWN_ITEMS:
        return [CONVERT if row["view_mode"] != summary_module.SUMMARY else REWRITE], None
    # A conversion that stopped after its commit still archives and removes its item files.
    return ([CONVERT] if row["conversion"] is not None else []), None


def _unsettled(root, conn, cid):
    """Whether a view of ``cid`` is unsettled: unpublished, held, or not the bytes the store published.

    It reads what the conversion's preflight reads, without the edit-back it would start.
    """
    return conversion.unsettled(conn, cid) or conversion.first_edited(
        root, conversion.published_views(conn, cid, ("manifest", "item"))) is not None


def _stored(root, conn, cid):
    """``cid``'s current manifest, its type and its stored text in ``conn``, as the conversion reads them."""
    path, text = conn.execute(
        "SELECT c.manifest_path, m.manifest_text FROM collections c JOIN collection_manifests m "
        "ON m.collection_id=c.collection_id AND m.manifest_version=c.manifest_version WHERE c.collection_id=?",
        (cid,)).fetchone()
    current = collections.parse_manifest_bytes(root, path, text.encode())
    return current, types.type_for_manifest(current), text


def _conversion_blocker(root, conn, row):
    """What stops this collection's conversion that a preview can see, or None, and the warnings it carries.

    ``conn`` is the live store, or the proof import for a file collection, which has no views yet.
    The manifest checks and the disclosure check are the conversion's own (``conversion.summary_form``).
    A warning is a saved view that the collection already cannot serve; the conversion keeps it as it is.
    """
    cid, path = row["collection_id"], row["path"]
    if row["authority"] == inventory.STORE and _unsettled(root, conn, cid):
        return _blocker("COLLECTION_CONVERSION_OUTSTANDING", f"a view of the collection is not settled; {_SETTLE}",
                        path), []
    operation = governance.OperationAuthorization(root, conn, mutation=False)
    try:
        _, proposed = conversion.summary_form(root, operation, *_stored(root, conn, cid))
    except (collections.CollectionError, types.CollectionTypeError) as error:
        return _blocker(error.code, error.reason, path), []
    finally:
        operation.close()
    return None, [{"code": found.code, "reason": found.reason, "location": found.location}
                  for found in proposed.view_diagnostics]


def _vault_blockers(root, steps):
    """What stops the whole apply: the release, and the coordinator every offline step needs."""
    found = []
    try:
        capability.require_records_summary(root)
    except collections.CollectionError as error:
        found.append(_blocker(error.code, error.reason))
    if MIGRATE in steps and not capability.collections_migrate_enabled():
        found.append(_blocker(capability.MIGRATION_UNAVAILABLE, "this release has not enabled the collections "
                              f"migration, which the cutover uses for file collections ({capability.COLLECTIONS_MIGRATE_V1})"))
    coordinator = preflight.coordinator_capability()
    if coordinator["status"] == "not_configured":
        refusal = admission.coordination_required("the collections cutover")
        found.append(_blocker(refusal.code, refusal.reason))
    return found


def _bodies(conn, cid):
    """How many items of ``cid`` carry body text, which after the conversion only the store holds.

    None when ``conn`` holds no row of ``cid``: a dataset, or a file collection the preflight did not import.
    """
    if conn is None or not cid or conn.execute(
            "SELECT 1 FROM collections WHERE collection_id=?", (cid,)).fetchone() is None:
        return None
    return conn.execute("SELECT COUNT(*) FROM items WHERE collection_id=? "
                        "AND trim(body, ' ' || char(9, 10, 13)) != ''", (cid,)).fetchone()[0]


def _sealed(root, proof, rows, staging):
    """The plan: each collection with its steps and blocker, the vault's blockers, and its ``plan_id``."""
    planned, blockers = [], []
    with ExitStack() as stack:
        live = None
        if proof.marker is not None and connection.store_path(root).exists():
            live = stack.enter_context(connection.reader_snapshot(connection.store_path(root)))
        for row in rows:
            steps, blocker = _steps(row)
            warnings = []
            # A file collection's rows are in the proof import; a store collection's in the live store.
            held = staging if row["authority"] == inventory.FILE else live
            if blocker is None and steps and row["conversion"] is None:
                blocker, warnings = _conversion_blocker(root, held, row)
            if blocker is not None:
                blockers.append(blocker)
            planned.append({name: row[name] for name in (
                "path", "collection_id", "authority", "storage_strategy", "view_mode", "rows", "conversion")}
                | {"steps": steps if blocker is None else [], "blocker": blocker, "warnings": warnings,
                   "bodies": _bodies(held, row["collection_id"])})
    work = {step for row in planned for step in row["steps"]}
    if work:
        blockers.extend(_vault_blockers(root, work))
    entries = [(row["path"], row["collection_id"] or "", row["authority"], row["storage_strategy"] or "",
                row["view_mode"] or "", ",".join(row["steps"]), (row["blocker"] or {}).get("code") or "")
               for row in planned]
    entries.extend(("", "", "", "", "", "", blocker["code"]) for blocker in blockers if blocker["path"] is None)
    return {"mode": MODE, "plan_id": plan_identity(MODE, {}, entries), "collections": planned, "blockers": blockers}


def _outcome(sealed):
    moving = sum(1 for row in sealed["collections"] if row["steps"])
    if sealed["blockers"]:
        return (f"The cutover is blocked by {len(sealed['blockers'])} blocker(s); each names its fix. "
                "Fix them, then preview again.")
    if not moving:
        return "Every structured collection is already in SQLite-only storage, or stays in files; nothing to do."
    carried = sum(len(row["warnings"]) for row in sealed["collections"])
    warned = (f" {carried} saved view(s) that already cannot be served move unchanged; each collection's warnings "
              "name them, so revise them after the cutover." if carried else "")
    return (f"{moving} collection(s) would move to SQLite-only storage.{warned} The owner applies this plan_id on the "
            f"host: {APPLY}.")


def preview(vault_root) -> dict:
    """The read-only cutover plan with its ``plan_id``; the vault, the store and the lease stay untouched.

    A run that stopped short previews itself instead, so the owner finishes it with its own ``plan_id``.
    """
    admission.require_owner(vault_root, "the collections cutover")
    root = Path(vault_root).resolve()
    run = _unfinished(root)
    if run is not None:
        return {"mode": MODE, "step": "resume", "dry_run": True, "plan_id": run["plan_id"], "phase": run["phase"],
                "outcome": (f"A cutover stopped at phase {run['phase']}. Apply its plan_id again to finish it, "
                            f"with --backup-to naming a new file for the resume's own snapshot: {APPLY}."),
                "collections": [{name: entry[name] for name in ("path", "collection_id", "steps", "done")}
                                for entry in run["collections"]],
                "blockers": []}
    with _surveyed(root) as (proof, rows, staging):
        sealed = _sealed(root, proof, rows, staging)
    return {**sealed, "step": "preview", "dry_run": True, "outcome": _outcome(sealed)}


# Parity


def _digest(cursor):
    digest = hashlib.sha256()
    for row in cursor:
        digest.update(json.dumps(row, separators=(",", ":")).encode())
        digest.update(b"\n")
    return digest.hexdigest()


def measure(conn, cid):
    """The parity measures of one collection in ``conn``: the live store, its snapshot or a proof import.

    Derived rows live beside the store and rebuild from its import log, so the log stands for them:
    its row count and member digests. They have no item keys, so ``keys`` is ``None``.
    """
    if derived_rows.is_derived(conn, cid):
        rows = derived_rows.logged(conn, cid)[1]
        keys = None
        payload = _digest(conn.execute("SELECT seq,member_sha256,rows_digest FROM import_members "
                                       "WHERE collection_id=? ORDER BY seq", (cid,)))
    else:
        rows = conn.execute("SELECT COUNT(*) FROM items WHERE collection_id=?", (cid,)).fetchone()[0]
        keys = _digest(conn.execute("SELECT item_key FROM items WHERE collection_id=? ORDER BY item_key", (cid,)))
        payload = _digest(conn.execute("SELECT item_key,payload_hash FROM items WHERE collection_id=? "
                                       "ORDER BY item_key", (cid,)))
    audit = conn.execute("SELECT COUNT(*) FROM txns WHERE collection_id=?", (cid,)).fetchone()[0]
    return {"rows": rows, "keys": keys, "payload_hashes": payload, "audit_events": audit}


def _parity(entry, found, *, added):
    """Refuse unless ``found`` equals the collection's baseline, with ``added`` audit events of this run."""
    expected = {**entry["baseline"], "audit_events": entry["baseline"]["audit_events"] + added}
    for name in MEASURES:
        if found[name] != expected[name]:
            raise CollectionStoreError(
                PARITY, f"{entry['path']}: {name} is {found[name]} where the backup measured {expected[name]}; "
                "the run stopped, and this collection's files and every later collection are as they were. "
                "Keep the backup, find what changed the collection, and run the apply again")
    return {"status": "held", **found, "audit_events": {"before": entry["baseline"]["audit_events"],
                                                        "after": found["audit_events"]}}


def _store_measures(root, cid):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        return measure(reader, cid)


# Run record


def _runs(root):
    return state_paths.vault_state_dir(root) / conversion.ARCHIVE_TREE / RUNS


def _record_path(root, plan_id):
    if not isinstance(plan_id, str) or len(plan_id) != 64 or any(c not in "0123456789abcdef" for c in plan_id):
        raise CollectionStoreError("INVALID_ARGUMENTS", "plan_id is not a collections-cutover plan_id; preview first")
    return _runs(root) / f"{plan_id}.json"


def _load(path):
    run = json.loads(path.read_bytes())
    if run.get("version") != 1 or run.get("phase") not in (BACKED_UP, MIGRATED, COMPLETE):
        raise CollectionStoreError("COLLECTIONS_CUTOVER_RECORD_INVALID", f"the cutover record {path} cannot be read")
    return run


def _unfinished(root):
    folder = _runs(root)
    found = [_load(path) for path in sorted(folder.glob("*.json"))] if folder.is_dir() else []
    return next((run for run in found if run["phase"] != COMPLETE), None)


def _write(root, run):
    conversion._durable(_record_path(root, run["plan_id"]), json.dumps(run, indent=1, sort_keys=True).encode())


# Apply


def _backup(root, destination):
    """The verified backup the parity baselines come from, or why there is none."""
    if not connection.store_path(root).exists():
        return None, {"status": "none", "reason": "this host had no collection store yet, so every collection was "
                      "in files; each file is archived and verified before it leaves the vault"}
    if destination is None:
        raise CollectionStoreError(BACKUP_REQUIRED, "the cutover measures the collections it changes from a backup "
                                   "snapshot it takes first; name a new file outside the vault with --backup-to")
    target = Path(destination).expanduser()
    # Never over a file: it may be this run's first backup, the state before any step, or the owner's own copy.
    if os.path.lexists(target):
        raise CollectionStoreError(BACKUP_EXISTS, f"{target} already exists, and the cutover never replaces a file; "
                                   "name a file that does not exist yet with --backup-to")
    from . import owner

    taken = owner.backup(root, destination=destination)
    if conversion._file_sha(Path(taken["path"])) != taken["sha256"]:
        raise CollectionStoreError(BACKUP_CHANGED, f"the backup at {taken['path']} does not verify after writing")
    derived = derived_rows.fingerprint(connection.store_path(root))
    return Path(taken["path"]), {"status": "verified", **taken, "derived": None if derived is None else {
        **derived, "copied": False, "reason": DERIVED_NOT_COPIED}}


def _begin(root, sealed, staging, plan_id, why, destination):
    """Take and verify the backup, measure each collection the plan changes, and record the run."""
    snapshot_path, backup = _backup(root, destination)
    with ExitStack() as stack:
        snapshot = None if snapshot_path is None else stack.enter_context(
            closing(connection.open_reader(snapshot_path)))
        entries = [{"path": row["path"], "collection_id": row["collection_id"], "steps": row["steps"],
                    # A conversion committed before the backup already counts its audit event there.
                    "converting": row["conversion"] is not None,
                    "baseline": measure(staging if MIGRATE in row["steps"] else snapshot, row["collection_id"]),
                    "done": False, "parity": None, "archive": None, "archived": None, "removed": None,
                    "not_removed": None, "projection": None}
                   for row in sealed["collections"] if row["steps"]]
    run = {"version": 1, "mode": MODE, "plan_id": plan_id, "why": why, "phase": BACKED_UP,
           "started_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "backup": backup,
           "collections": entries}
    _write(root, run)
    return run


def _routed(root, entries):
    proof = ownership.current(root)
    return all((found := proof.selected(root, entry["path"])) is not None and found["collection_id"] in proof.admitted
               for entry in entries)


def _from_files(run, entry):
    """Whether ``entry``'s measures come from a proof import of its files: its migration has not been checked."""
    return MIGRATE in entry["steps"] and run["phase"] == BACKED_UP


def _untouched(root, live, run, entry):
    """Whether no step of this run has changed ``entry``'s collection yet, so a resume may measure it again.

    A file collection counts as changed once the migration routes it, until the migration's parity
    check. A conversion this run committed changed the collection; one committed before the run
    (``converting``) adds no audit event when the run finishes it.
    """
    if entry["parity"] is not None:
        return False
    if _from_files(run, entry):
        return not _routed(root, [entry])
    if entry["converting"]:
        return True
    cid = entry["collection_id"]
    return conversion.record(live, cid) is None and conversion.convertible(_stored(root, live, cid)[0])


def _remeasure(root, run, destination):
    """On a resume, measure each collection the run has not changed yet again, from a new verified snapshot.

    What stopped the run may have needed a change to a collection, which its first measures do not
    hold. Each later step compares with the state it starts from. The first backup stays the state
    before the run, and the receipt names it and each resume's snapshot.
    """
    store = connection.store_path(root)
    with ExitStack() as stack:
        live = stack.enter_context(closing(connection.open_reader(store))) if store.exists() else None
        pending = [entry for entry in run["collections"] if _untouched(root, live, run, entry)]
    if not pending:
        return
    snapshot_path, backup = _backup(root, destination)
    with ExitStack() as stack:
        snapshot = None if snapshot_path is None else stack.enter_context(
            closing(connection.open_reader(snapshot_path)))
        staging = None
        if any(_from_files(run, entry) for entry in pending):
            _, _, staging = stack.enter_context(_surveyed(root))
            if staging is None:
                raise CollectionStoreError(MIGRATION_INCOMPLETE, "the files of a collection the migration has not "
                                           "moved yet no longer import; run `exomem collections migrate --dry-run` "
                                           "for its blockers, then apply again")
        for entry in pending:
            entry["baseline"] = measure(staging if _from_files(run, entry) else snapshot, entry["collection_id"])
    run.setdefault("resumes", []).append({"backup": backup, "measured": [entry["path"] for entry in pending]})
    _write(root, run)


def _migrate(root, run):
    pending = [entry for entry in run["collections"] if MIGRATE in entry["steps"]]
    if pending and not _routed(root, pending):
        result = admission.migrate_route(root, step="apply")
        if not _routed(root, pending):
            raise CollectionStoreError(MIGRATION_INCOMPLETE, "the migration did not move every file collection into "
                                       f"the store (verdict {result.get('verdict')}, status {result.get('status')}); "
                                       "run `exomem collections migrate --dry-run` for its blockers, then apply again")
    # A migration changes no store collection, and imports each file collection as the proof import did.
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        for entry in run["collections"]:
            _parity(entry, measure(reader, entry["collection_id"]), added=0)
    run["phase"] = MIGRATED
    _write(root, run)


def _building(root, cid):
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        # A building candidate is always the newest attempt, so the newest unready state names it.
        return index_migrations.unready_state(reader, cid) == "building"


def _publish_projection(root, writer, cid):
    """Backfill the projection that ``cid``'s conversion began until it publishes; its final state.

    FAILED when the build fails a stored value: the old projection stays, and a Planning write reads every row.
    """
    from . import runtime
    from .preview import _mutate

    state = READY  # Nothing building: the conversion found the projection of its declaration current.
    while _building(root, cid):
        published, _ = _mutate(root, writer.backfill_derived, cid, limit=runtime.BACKFILL_BATCH_ROWS, rollup=False)
        # A batch that neither publishes nor leaves the candidate building has failed it.
        state = READY if published else FAILED
    return state


def _archived(root, cid):
    """The archive counts of a conversion that finished after the run's last record, or None where unknown.

    Its verified archive manifest names the files it archived. Which of them the removal kept went
    with the receipt the cut lost, so ``removed`` and ``not_removed`` stay unknown.
    """
    folder = state_paths.vault_state_dir(root) / conversion.ARCHIVE_TREE / cid
    found = [(path, held) for path in sorted(folder.iterdir()) if (held := conversion._verified(path)) is not None] \
        if folder.is_dir() else []
    if len(found) != 1:
        return dict.fromkeys(COUNTS)
    path, (files, _) = found[0]
    return {"archive": str(path), "archived": len(files), "removed": None, "not_removed": None}


def _convert_all(root, run):
    """Convert each collection the plan converts, in the owner's offline session, recording each one done."""
    from ..governance import principal
    from .export import _files_as
    from .preview import bound_writer, preview_store

    def work(session, manager, fence_client):
        # Archiving many item files can outlast one lease; the offline owner stops renewal at its close.
        manager.start_renewer()
        opened = admission.open_store(session, manager, fence_client=fence_client)
        if opened["status"] != "admitted":
            raise session.runtime(retire=False)._refusal()
        runtime = session.runtime()
        with manager.writer_authority_guard(vault_root=root):
            token = manager._fencing_token
        with principal.library_scope(), _files_as(session, token), preview_store(root, runtime):
            for entry in run["collections"]:
                if entry["done"]:
                    continue
                if entry["parity"] is None:
                    result = conversion._convert(root, entry["collection_id"], run["why"], check=(
                        lambda conn, cid, entry=entry: _parity(entry, measure(conn, cid), added=0)))
                    # The conversion's own revise event, unless it committed before the backup. A run cut
                    # after this commit finds the collection converted, with that one event.
                    parity = _parity(entry, _store_measures(root, entry["collection_id"]),
                                     added=0 if entry["converting"] else 1)
                    # Recorded before the projection build, so a run cut during it keeps what was archived.
                    entry.update(parity=parity, **(_archived(root, entry["collection_id"])
                                                   if result["outcome"] == "already_converted" else
                                                   {name: result[name] for name in COUNTS}))
                    _write(root, run)
                entry.update(done=True, projection=_publish_projection(root, bound_writer(root), entry["collection_id"]))
                _write(root, run)
        return {"status": "converted"}

    admission._offline_owner(root, "cutover", work)
    run["phase"] = COMPLETE
    moved = len(run["collections"])
    failed = [entry["path"] for entry in run["collections"] if entry["projection"] == FAILED]
    # A run that an earlier release began may hold a copied derived file in its backup.
    derived = run["backup"].get("derived")
    left_out = (f"; it leaves out the derived rows in {derived['path']}, which rebuild from the store's import log."
                if derived is not None and derived.get("copied") is False else ".")
    run["outcome"] = (f"Moved {moved} collection(s) to SQLite-only storage; parity held for every one. "
                      + (f"The query index of {', '.join(failed)} failed to build; a revise of the collection's "
                         "declaration builds it again. " if failed else "")
                      + (f"The backup from before the run is {run['backup']['path']}{left_out}"
                         if run["backup"]["status"] == "verified"
                         else "No backup was taken: " + run["backup"]["reason"] + ".")
                      + "".join(f" A resume measured {', '.join(resume['measured'])} again from its snapshot "
                                f"{resume['backup']['path']}." for resume in run.get("resumes", [])
                                if resume["backup"]["status"] == "verified"))
    _write(root, run)


def apply(vault_root, *, plan_id, why, backup_to=None) -> dict:
    """Apply the previewed ``plan_id``, or resume or repeat its run; the finished run record is the receipt."""
    admission.require_owner(vault_root, "the collections cutover")
    root = Path(vault_root).resolve()
    path = _record_path(root, plan_id)
    run = _load(path) if path.exists() else None
    if run is not None and run["phase"] == COMPLETE:
        return run
    records._validate_why(why)
    config = writer_lease.LeaseConfig.from_env()
    if config.enabled:
        # Before the backup, so that no other writer changes a collection between its measures and its steps.
        admission._require_no_service(config, "the collections cutover")
    if run is None:
        unfinished = _unfinished(root)
        if unfinished is not None:
            raise CollectionStoreError(
                "COLLECTIONS_CUTOVER_UNFINISHED", f"the cutover {unfinished['plan_id']} stopped at phase "
                f"{unfinished['phase']}; apply that plan_id to finish it before another")
        with _surveyed(root) as (proof, rows, staging):
            sealed = _sealed(root, proof, rows, staging)
            # Drift first: a plan the owner did not review is stale, whatever blocks the new one.
            require_current_plan(MODE, plan_id, sealed["plan_id"], "the collections, their steps or their blockers")
            if sealed["blockers"]:
                raise CollectionStoreError(BLOCKED, "the cutover is blocked: " + "; ".join(
                    f"{blocker['path'] or 'the vault'} ({blocker['code']}: {blocker['reason']})"
                    for blocker in sealed["blockers"]))
            run = _begin(root, sealed, staging, plan_id, why, backup_to)
    else:
        if run["backup"]["status"] == "verified" and conversion._file_sha(Path(run["backup"]["path"])) != run["backup"]["sha256"]:
            raise CollectionStoreError(BACKUP_CHANGED, f"the backup at {run['backup']['path']} no longer verifies; "
                                       "restore its bytes, or keep a copy, before the run continues")
        _remeasure(root, run, backup_to)
    if run["phase"] == BACKED_UP:
        _migrate(root, run)
    if run["phase"] == MIGRATED:
        _convert_all(root, run)
    return run
