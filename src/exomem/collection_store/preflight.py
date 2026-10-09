"""Read-only migration preflight: would each file collection import and prove cleanly?

OpenSpec move-structured-collections-to-sqlite P1b.1-2 and design §10. Each file
Records or Planning collection is captured with the legacy reader, imported into an
unpublished staging store in private scratch under the vault's state directory, and
put through the round-trip proof (a)-(f). The preflight writes no vault file, no live
store and no marker, and the scratch goes with the call. It migrates nothing: applying
belongs to the declared offline migration (P1b.3).
"""

from __future__ import annotations

import datetime as dt
import tempfile
import time
import uuid
from pathlib import Path

from .. import state_paths, vault
from .. import structured_collections as collections
from . import admission, authority, connection, legacy, legacy_import

#: Seconds for the whole preflight: one audit scan plus every collection's capture and proof.
DEADLINE_SECONDS = 300
_ACTOR = "collections-store-preflight"
#: What capture, import and proof raise for one collection's unusable input: the spool's own
#: declared input failures, a vault path guard and a collection refusal. Other errors are faults.
_REFUSALS = (*legacy._INPUT_FAILURES, vault.PathGuardError, collections.CollectionError)
_DEADLINE = {"code": "COLLECTION_PREFLIGHT_DEADLINE",
             "reason": "the preflight deadline elapsed before this collection was checked"}


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
    collection is blocked.
    """
    admission.require_owner(vault_root, "the collections-store preflight")
    root = Path(vault_root).resolve()
    raw = authority.read_marker(root)
    marker = None if raw is None else authority.parse_marker(root, raw)

    def routed(path):
        return marker is not None and authority.selected_entry(root, marker, path) is not None

    store_ids = set() if marker is None else {entry["collection_id"] for entry in marker["collections"]}
    # Duplicates come through so that every copy can be named as a blocker.
    manifests, unreadable = collections.discover_collections_with_errors(root, reject_duplicates=False)
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
    pending = sorted((manifest for manifest in pending if legacy_import.importable(manifest)),
                     key=lambda found: found.path)
    if pending:
        rows.extend(_prove_all(root, pending))
    rows.sort(key=lambda row: row["path"])
    blocked = [row["path"] for row in rows if row["status"] == "blocked"]
    ready = [row for row in rows if row["status"] == "ready"]
    return {"mode": "collections-store", "dry_run": True,
            "verdict": "blocked" if blocked else "migratable" if ready else "nothing_to_migrate",
            "blocked": blocked, "collections": rows, "store_collections": len(store_ids)}


def _prove_all(root, manifests) -> list[dict]:
    context = legacy_import.ImportContext(
        _ACTOR, dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), uuid.uuid4().hex)
    rows = []
    with tempfile.TemporaryDirectory(prefix="collections-preflight-",
                                     dir=state_paths.ensure_vault_state_dir(root)) as scratch, \
            connection.staging_store(Path(scratch) / "staging.sqlite") as staging:
        spool = Path(scratch) / "audit"
        spool.mkdir(mode=0o700)
        with legacy.LegacyAuditSpool(spool, deadline=time.monotonic() + DEADLINE_SECONDS) as audit:
            try:
                audit.scan(root)
            except TimeoutError:
                return [_row(manifest.path, manifest, status="blocked", blocker=_DEADLINE)
                        for manifest in manifests]
            except _REFUSALS as error:
                # Without the audit census no collection can be proved.
                blocker = _blocker(error)
                blocker["reason"] = "the vault's audit history could not be read: " + blocker["reason"]
                return [_row(manifest.path, manifest, status="blocked", blocker=blocker)
                        for manifest in manifests]
            for index, manifest in enumerate(manifests):
                try:
                    rows.append(_prove_one(root, manifest, staging, audit, context))
                except TimeoutError:
                    # The spool is spent: this and every later collection is unchecked, not clean.
                    rows.extend(_row(later.path, later, status="blocked", blocker=_DEADLINE)
                                for later in manifests[index:])
                    break
    return rows


def _prove_one(root, manifest, staging, audit, context) -> dict:
    try:
        captured = legacy_import.capture_legacy_collection(root, manifest.path, audit=audit)
    except TimeoutError:
        raise
    except _REFUSALS as error:
        return _row(manifest.path, manifest, status="blocked", blocker=_blocker(error))
    found = {"rows": len(captured.snapshot.records),
             "legacy_audit_status": captured.legacy_inspection["status"]}
    staging.execute("BEGIN IMMEDIATE")
    try:
        result = legacy_import.import_legacy_collection(staging, captured, audit=audit, context=context)
    except TimeoutError:
        staging.execute("ROLLBACK")
        raise
    except _REFUSALS as error:
        staging.execute("ROLLBACK")
        return _row(manifest.path, manifest, status="blocked", blocker=_blocker(error), **found)
    except BaseException:
        staging.execute("ROLLBACK")
        raise
    # Kept, so that a later collection whose source path collides is named as a conflict.
    staging.execute("COMMIT")
    return _row(manifest.path, manifest, status="ready", proof="passed",
                legacy_events=result.legacy_event_count, **found)
