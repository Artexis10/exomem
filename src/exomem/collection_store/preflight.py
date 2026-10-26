"""Read-only migration preflight: would each file collection import and prove cleanly?

OpenSpec move-structured-collections-to-sqlite P1b.1-2 and design §10. Each file
Records or Planning collection is captured with the legacy reader, imported into an
unpublished staging store in private scratch under the vault's state directory, and
put through the round-trip proof (a)-(f). The preflight writes no vault file, no live
store and no marker, and the scratch goes with the call. It migrates nothing: applying
belongs to the declared offline migration (P1b.3, ``migration.py``), which reuses these
steps. Where writer coordination is configured, it first probes the coordinator's store
fence (ruled N3).
"""

from __future__ import annotations

import datetime as dt
import tempfile
import time
import uuid
from contextlib import contextmanager
from importlib import metadata
from pathlib import Path

from .. import state_paths, vault, writer_lease
from .. import structured_collections as collections
from ..cli_ops import OpError
from . import admission, authority, connection, legacy, legacy_import

#: Seconds for the whole preflight: one audit scan plus every collection's capture and proof.
DEADLINE_SECONDS = 300
_ACTOR = "collections-store-preflight"
#: What capture, import and proof raise for one collection's unusable input: the spool's own
#: declared input failures, a vault path guard and a collection refusal. Other errors are faults.
_REFUSALS = (*legacy._INPUT_FAILURES, vault.PathGuardError, collections.CollectionError)
_DEADLINE = {"code": "COLLECTION_PREFLIGHT_DEADLINE",
             "reason": "the preflight deadline elapsed before this collection was checked"}
UPGRADE_REQUIRED = "COLLECTION_STORE_COORDINATOR_UPGRADE_REQUIRED"


def _running_release() -> str:
    try:
        return metadata.version("exomem")
    except metadata.PackageNotFoundError:
        return "the release running here (its installed version is unrecorded)"


def coordinator_capability(fence_client=None) -> dict:
    """Probe the coordinator's collection-store fence route explicitly (ruled N3).

    A coordinator that silently ignores an extra acquire field proves nothing, so this
    reads the fence itself, in single-host and multi-host topologies alike. A coordinator
    without the route refuses with the release to upgrade it to: this one, which serves
    ``collections-store-v1``. That prevents a store that no coordinator fence protects
    from old writers; a false refusal costs an owner one coordinator upgrade.
    """
    if fence_client is None:
        config = writer_lease.LeaseConfig.from_env()
        if not config.enabled:
            return {"status": "not_configured"}
        fence_client = writer_lease.LeaseCoordinatorClient(config)
    try:
        fence = fence_client.collection_store_fence()
    except OpError as error:
        if error.code != "WRITER_COORDINATOR_CONTRACT_ABSENT":
            raise
        raise connection.CollectionStoreError(
            UPGRADE_REQUIRED,
            f"the writer-lease coordinator does not serve the {writer_lease.COLLECTION_STORE_CAPABILITY} "
            f"store fence; upgrade it to Exomem {_running_release()}, the release running here") from error
    return {"status": "supported", "capability": fence.capability, "enrolled": fence.enrolled}


def _blocker(error) -> dict:
    found = {"code": getattr(error, "code", None) or type(error).__name__,
             "reason": getattr(error, "reason", None) or getattr(error, "detail", None) or str(error)}
    if isinstance(error, legacy_import.LegacyProofError):
        found["check"] = error.check
    return found


def _row(path, manifest=None, *, status, blocker=None, **found) -> dict:
    row = {"path": path, "status": status,
           "collection_id": None if manifest is None else manifest.collection_id,
           "semantic_profile": None if manifest is None else manifest.semantic_profile,
           "layout": None if manifest is None else manifest.storage.strategy, **found}
    if blocker is not None:
        row["blocker"] = blocker
    return row


def preflight(vault_root) -> dict:
    """Report, per file collection, its rows, legacy audit status, proof result and blockers.

    Owner-only, because it reads every collection with full authority. The report holds
    counts, statuses, codes and paths, never row values. A dataset is skipped, because it
    keeps file authority. Collections the vault's marker routes to the store are counted, not
    listed: their paths are store-owned. The vault is ``migratable`` only when no file
    collection is blocked and a writer-lease coordinator is configured; without one, a vault
    with something left to migrate is ``blocked`` with a vault-level ``blocker``.
    """
    admission.require_owner(vault_root, "the collections-store preflight")
    root = Path(vault_root).resolve()
    try:
        coordinator = coordinator_capability()
    except OpError as error:
        # An outage leaves the collections' answer standing; only the coordinator's is unknown.
        coordinator = {"status": "unavailable", "code": error.code}
    rows, pending, store_ids = candidates(root)
    if pending:
        rows.extend(_prove_all(root, pending))
    found = report(rows, store_ids)
    if coordinator["status"] == "not_configured" and found["verdict"] != "nothing_to_migrate":
        # The apply would refuse, so the vault is not migratable here, whatever its collections prove.
        refusal = admission.coordination_required("the collections-store migration")
        found = {**found, "verdict": "blocked", "blocker": {"code": refusal.code, "reason": refusal.reason}}
    return {**found, "dry_run": True, "coordinator": coordinator}


def candidates(root, *, ownership_proof=None):
    """Find file candidates outside the complete canonical namespaces.

    Private restore staging supplies one proof from its verified snapshot, only
    while that context is open. It grants no admission or view access. Ordinary
    callers obtain the current guarded production proof.
    """
    from . import ownership

    proof = ownership.current(root) if ownership_proof is None else ownership_proof

    def routed(path):
        return proof.selected(root, path) is not None

    store_ids = {entry["collection_id"] for entry in proof.entries}
    # Duplicates come through so that every copy can be named as a blocker.
    manifests, unreadable = collections.discover_collections_with_errors(
        root, reject_duplicates=False, ownership_proof=proof)
    rows = [_row(entry.path, status="blocked", blocker={"code": entry.code, "reason": entry.message})
            for entry in unreadable if not routed(entry.path)]
    shared = {}
    for manifest in manifests:
        if not routed(manifest.path):
            shared.setdefault(manifest.collection_id, []).append(manifest)
    pending = []
    for collection_id, group in shared.items():
        try:
            # A file copy of a store collection's identity is a duplicate too.
            collections.raise_duplicate_ids([*(manifest.collection_id for manifest in group),
                                             *({collection_id} & store_ids)])
        except collections.CollectionError as error:
            # Every copy is blocked: which folder the owner meant to keep is theirs to decide.
            rows.extend(_row(manifest.path, manifest, status="blocked", blocker=_blocker(error))
                        for manifest in group)
            continue
        pending.extend(group)
    rows.extend(_row(manifest.path, manifest, status="skipped", reason="a dataset keeps file authority")
                for manifest in pending if not legacy_import.importable(manifest))
    staying = [authority.file_ownership(manifest) for manifest in pending if not legacy_import.importable(manifest)]
    staying.extend(proof.entries)
    pending = sorted((manifest for manifest in pending if legacy_import.importable(manifest)),
                     key=lambda found: found.path)
    # Each migrated collection becomes a marker entry; entries may not share an ownership
    # namespace with each other or with a routed or dataset collection, as the apply refuses.
    overlapping = {manifest.path for manifest in pending
                   if any(authority._overlap(authority.file_ownership(manifest), other) for other in staying)
                   or any(authority._overlap(authority.file_ownership(manifest), authority.file_ownership(peer))
                          for peer in pending if peer is not manifest)}
    rows.extend(_row(manifest.path, manifest, status="blocked", blocker={
        "code": "COLLECTION_STORE_MARKER_CONFLICT", "reason": "collection ownership namespaces overlap"})
        for manifest in pending if manifest.path in overlapping)
    return rows, [manifest for manifest in pending if manifest.path not in overlapping], store_ids



def report(rows, store_ids) -> dict:
    rows.sort(key=lambda row: row["path"])
    blocked = [row["path"] for row in rows if row["status"] == "blocked"]
    ready = [row for row in rows if row["status"] == "ready"]
    return {"mode": "collections-store",
            "verdict": "blocked" if blocked else "migratable" if ready else "nothing_to_migrate",
            "blocked": blocked, "collections": rows, "store_collections": len(store_ids)}


def import_context(actor=_ACTOR):
    return legacy_import.ImportContext(
        actor, dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), uuid.uuid4().hex)


def _prove_all(root, manifests) -> list[dict]:
    with tempfile.TemporaryDirectory(prefix="collections-preflight-",
                                     dir=state_paths.ensure_vault_state_dir(root)) as scratch, \
            connection.staging_store(Path(scratch) / "staging.sqlite") as staging:
        return prove_into(root, manifests, staging, Path(scratch), import_context())


def _scan(audit, root) -> None:
    """Scan the vault's audit history; once more when a write to the vault outran the first scan."""
    try:
        audit.scan(root)
    except connection.CollectionStoreError as error:
        if error.code != legacy.SOURCE_CHANGED:
            raise
        audit.refresh(root)


def capture_collection(root, manifest_path, *, audit, presentation_operation):
    """``capture_legacy_collection``, once more after a fresh audit scan when a write outran it.

    Every governed write rewrites the audit log, so a capture beside a live writer is outrun
    by it. Being outrun twice raises the same SOURCE_CHANGED refusal.
    """
    try:
        return legacy_import.capture_legacy_collection(
            root, manifest_path, audit=audit, presentation_operation=presentation_operation)
    except connection.CollectionStoreError as error:
        if error.code != legacy.SOURCE_CHANGED:
            raise
    audit.refresh(root)
    return legacy_import.capture_legacy_collection(
        root, manifest_path, audit=audit, presentation_operation=presentation_operation)


def retried(root, audit, row, again) -> dict:
    """``row``, or ``again()`` when a write to the vault outran its capture or proof, as `capture_collection`.

    Being outrun a second time, or a failed fresh scan, keeps the first row's SOURCE_CHANGED block.
    """
    if row.get("blocker", {}).get("code") != legacy.SOURCE_CHANGED:
        return row
    try:
        audit.refresh(root)
    except _REFUSALS:
        return row
    return again()


@contextmanager
def scanned(root, scratch, manifests, *, deadline=None):
    """One audit spool that scanned the vault, or ``None`` and a blocked row for every collection.

    The spool lives in the private directory ``scratch`` and goes with the context. Its scan,
    captures and imports stop at ``deadline`` (absolute monotonic), by default the preflight's.
    """
    deadline = time.monotonic() + DEADLINE_SECONDS if deadline is None else deadline
    with legacy.LegacyAuditSpool(Path(scratch), deadline=deadline) as audit:
        blocker = None
        try:
            _scan(audit, root)
        except TimeoutError:
            blocker = _DEADLINE
        except _REFUSALS as error:
            # Without the audit census no collection can be proved.
            blocker = _blocker(error)
            blocker["reason"] = "the vault's audit history could not be read: " + blocker["reason"]
        if blocker is not None:
            yield None, [_row(manifest.path, manifest, status="blocked", blocker=blocker) for manifest in manifests]
        else:
            yield audit, None


def prove_into(root, manifests, staging, scratch, context, *, record=None, deadline=None) -> list[dict]:
    """Capture, import and prove each collection into ``staging``, one committed transaction each.

    ``record(captured, result)`` runs inside a ready collection's transaction, so what it
    writes commits with that collection's import. ``deadline`` is ``scanned``'s.
    """
    with scanned(root, scratch, manifests, deadline=deadline) as (audit, blocked), \
            legacy_import.presentation_context(root, manifests=manifests) as presentation:
        if audit is None:
            return blocked
        rows = []
        for index, manifest in enumerate(manifests):
            try:
                rows.append(_prove_one(root, manifest, staging, audit, context, record,
                                       presentation_operation=presentation))
            except TimeoutError:
                # The spool is spent: this and every later collection is unchecked, not clean.
                rows.extend(_row(later.path, later, status="blocked", blocker=_DEADLINE)
                            for later in manifests[index:])
                break
        return rows


def _prove_one(root, manifest, staging, audit, context, record=None, *, presentation_operation=None) -> dict:
    def once():
        return _prove_once(root, manifest, staging, audit, context, record,
                           presentation_operation=presentation_operation)
    return retried(root, audit, once(), once)


def _prove_once(root, manifest, staging, audit, context, record=None, *, presentation_operation=None) -> dict:
    try:
        captured = legacy_import.capture_legacy_collection(root, manifest.path, audit=audit,
                                                          presentation_operation=presentation_operation)
    except TimeoutError:
        raise
    except _REFUSALS as error:
        return _row(manifest.path, manifest, status="blocked", blocker=_blocker(error))
    found = {"rows": len(captured.snapshot.records),
             "legacy_audit_status": captured.legacy_inspection["status"]}
    staging.execute("BEGIN IMMEDIATE")
    try:
        with legacy_import.checked_sql(staging, audit._check):
            result = legacy_import.import_legacy_collection(staging, captured, audit=audit, context=context)
            if record is not None:
                record(captured, result)
    except TimeoutError:
        if staging.in_transaction:
            staging.execute("ROLLBACK")
        raise
    except _REFUSALS as error:
        if staging.in_transaction:
            staging.execute("ROLLBACK")
        return _row(manifest.path, manifest, status="blocked", blocker=_blocker(error), **found)
    except BaseException:
        if staging.in_transaction:
            staging.execute("ROLLBACK")
        raise
    # Kept, so that a later collection whose source path collides is named as a conflict.
    staging.execute("COMMIT")
    return _row(manifest.path, manifest, status="ready", proof="passed",
                legacy_events=result.legacy_event_count, **found)
