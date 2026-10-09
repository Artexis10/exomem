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

from .. import state_paths
from .. import structured_collections as collections
from . import admission, authority, connection, legacy, legacy_import
from .connection import CollectionStoreError

#: Seconds for the whole preflight: one audit scan plus every collection's capture and proof.
DEADLINE_SECONDS = 300
_ACTOR = "collections-store-preflight"


def _blocker(error) -> dict:
    found = {"code": getattr(error, "code", "COLLECTION_PREFLIGHT_DEADLINE"),
             "reason": getattr(error, "reason", "the preflight deadline elapsed")}
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
    counts, statuses, codes and paths, never row values. A dataset is skipped (it keeps
    file authority), and a collection the vault's marker already routes to the store is
    reported as ``store``. The vault is ``migratable`` only when no collection is blocked.
    """
    admission.require_owner(vault_root, "the collections-store preflight")
    root = Path(vault_root).resolve()
    raw = authority.read_marker(root)
    routed = set() if raw is None else {
        entry["manifest_path"] for entry in authority.parse_marker(root, raw)["collections"]}
    # Duplicates come through so that every copy can be named as a blocker.
    manifests, unreadable = collections.discover_collections_with_errors(root, reject_duplicates=False)
    rows = [_row(entry.path, status="store" if entry.path in routed else "blocked",
                 blocker=None if entry.path in routed else {"code": entry.code, "reason": entry.message})
            for entry in unreadable]
    pending, shared = [], {}
    for manifest in sorted(manifests, key=lambda found: found.path):
        if manifest.path in routed:
            rows.append(_row(manifest.path, manifest, status="store"))
        else:
            shared.setdefault(manifest.collection_id, []).append(manifest)
    for group in shared.values():
        try:
            collections._raise_duplicate_ids(group)
        except collections.CollectionError as error:
            # Every copy is blocked: which folder the owner meant to keep is theirs to decide.
            rows.extend(_row(manifest.path, manifest, status="blocked", blocker=_blocker(error))
                        for manifest in group)
            continue
        pending.extend(group)
    pending.sort(key=lambda found: found.path)
    if pending:
        rows.extend(_prove_all(root, pending))
    rows.sort(key=lambda row: row["path"])
    blocked = [row["path"] for row in rows if row["status"] == "blocked"]
    ready = [row for row in rows if row["status"] == "ready"]
    return {"mode": "collections-store", "dry_run": True,
            "verdict": "blocked" if blocked else "migratable" if ready else "nothing_to_migrate",
            "blocked": blocked, "collections": rows}


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
            except (CollectionStoreError, TimeoutError) as error:
                # Without the audit census no collection can be proved.
                return [_row(manifest.path, manifest, status="blocked", blocker=_blocker(error))
                        for manifest in manifests]
            for index, manifest in enumerate(manifests):
                try:
                    rows.append(_prove_one(root, manifest, staging, audit, context))
                except TimeoutError as error:
                    # The spool is spent: this and every later collection is unchecked, not clean.
                    rows.extend(_row(later.path, later, status="blocked", blocker=_blocker(error))
                                for later in manifests[index:])
                    break
    return rows


def _prove_one(root, manifest, staging, audit, context) -> dict:
    try:
        captured = legacy_import.capture_legacy_collection(root, manifest.path, audit=audit)
    except CollectionStoreError as error:
        if error.code == "COLLECTION_LEGACY_IMPORT_UNSUPPORTED":
            return _row(manifest.path, manifest, status="skipped", reason=error.reason)
        return _row(manifest.path, manifest, status="blocked", blocker=_blocker(error))
    except collections.CollectionError as error:
        return _row(manifest.path, manifest, status="blocked", blocker=_blocker(error))
    found = {"rows": len(captured.snapshot.records),
             "legacy_audit_status": captured.legacy_inspection["status"]}
    staging.execute("BEGIN IMMEDIATE")
    try:
        result = legacy_import.import_legacy_collection(staging, captured, audit=audit, context=context)
    except (CollectionStoreError, collections.CollectionError) as error:
        staging.execute("ROLLBACK")
        return _row(manifest.path, manifest, status="blocked", blocker=_blocker(error), **found)
    except BaseException:
        staging.execute("ROLLBACK")
        raise
    # Kept, so that a later collection whose source path collides is named as a conflict.
    staging.execute("COMMIT")
    return _row(manifest.path, manifest, status="ready", proof="passed",
                legacy_events=result.legacy_event_count, **found)
