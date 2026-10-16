"""The reverse export of a migrated vault's store collections back to file-canonical files.

OpenSpec move-structured-collections-to-sqlite P1b.4, design §10 Reversal and §16 A5.
``preview`` plans read-only, without the lease: per routed collection it renders the
store's rows into the legacy layout, proves that each parses back to the same row, and
names every collection that cannot leave the store (``_unexportable``) or whose views a
person changed. ``apply`` runs in the owner's offline session, with the service stopped
and the operator credential. Under the boundary it plans again, records an export
intent in the store and publishes the replica, so no host serves or adopts a store
that is leaving. Per written collection it writes the rendered files, then one
content-free checkpoint (``records.export_checkpoint``), and it verifies that file mode
reads every collection as the store served it. Only then does it retire the
coordinator's fence, rename the marker to ``mode.json.exported-<utc>``, tombstone the
replica, retire the compatibility descriptors and, last, tombstone the live store. The
intent lives in the store until that final rename, so a run cut at any point resumes
from it. A collection the store never wrote keeps its imported files byte for byte.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing, contextmanager
from pathlib import Path, PurePosixPath

from .. import record_formats, records, state_migration, vault, writer_lease
from .. import structured_collections as collections
from ..cli_ops import OpError
from . import (
    admission,
    authority,
    chain,
    connection,
    derived_rows,
    legacy_import,
    owner,
    preflight,
    takeover,
    typed_storage,
)
from .connection import CollectionStoreError

#: The tombstone suffix: an export renames, never deletes, what no host may adopt again.
EXPORTED = ".exported-"
MARKER_LEAF = "mode.json"
_PLAN_DOMAIN = b"exomem.collection-store-export-plan:v1\0"
_TRANSITION_DOMAIN = b"exomem.collection-store-export-transition:v1\0"
#: The checkpoint's one gap code names the store transitions it summarizes.
_HISTORY_CODE = "collections-store-history:{store_id}:{first}-{last}"
#: The projection kinds the export renders back, of the schema's closed kinds; the others have no file form.
# nosemgrep: ep-word-set -- the schema's closed projection kinds that have a file form, not a vocabulary.
_RENDERED = ("manifest", "item", "log", "held")

STALE = "COLLECTION_STORE_EXPORT_STALE"
BLOCKED = "COLLECTION_STORE_EXPORT_BLOCKED"
UNVERIFIED = "COLLECTION_STORE_EXPORT_UNVERIFIED"
#: Why a collection cannot leave the store, as the preview names it: closed reasons of this module.
STORE_NATIVE, OVER_BOUND = "COLLECTION_STORE_EXPORT_STORE_NATIVE", "COLLECTION_STORE_EXPORT_OVER_BOUND"
UNREPRESENTABLE, VIEW_EDITED = "COLLECTION_STORE_EXPORT_UNREPRESENTABLE", "COLLECTION_STORE_EXPORT_VIEW_EDITED"
#: The refusal the log adapter raises at its byte bound while the plan parses a rendered log.
_LOG_BOUND = "RECORD_SOURCE_TOO_LARGE"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _blocker(code, reason):
    return {"code": code, "reason": reason}


def _read(root, relative):
    """A vault file's bytes, or None when it is absent."""
    try:
        return (Path(root) / relative).read_bytes()
    except FileNotFoundError:
        return None


def _unexportable(manifest, imported, rendered):
    """Why file mode cannot hold this collection, or None: the one rule a later ruling may relax.

    Pending the operator's ruling, a collection the store created (no legacy import) and
    one whose rendered files exceed a file-mode bound stay in the store, and so does the
    whole vault: a partial export would keep a store and its fence for the rest.
    ``rendered`` is None when the export writes nothing for the collection.
    """
    if imported is None:
        return _blocker(STORE_NATIVE, "the store created this collection, so file mode never held it")
    # A log's bound is the adapter's own: _render_log parses the rendered log through it.
    if rendered is None or manifest.storage.strategy == "markdown-log":
        return None
    # The items adapter checks its bounds inside its guarded directory walk and has no check
    # over planned files to reuse, so this counts what it counts: every file and directory
    # below the source, each file's bytes and their sum.
    source = PurePosixPath(manifest.storage.source)
    sources = [data for _, data in rendered["sources"]]
    directories = {parent for path, _ in rendered["sources"] for parent in PurePosixPath(path).parents
                   if source in parent.parents}
    bounds = ((len(sources) + len(directories), record_formats._MAX_ITEM_FILES, "item files and directories"),
              (max(map(len, sources), default=0), record_formats._MAX_ITEM_BYTES, "bytes in one item"),
              (sum(map(len, sources)), record_formats._MAX_COLLECTION_BYTES, "collection bytes"))
    for found, limit, name in bounds:
        if found > limit:
            return _blocker(OVER_BOUND, f"{found} {name} exceed the file-mode bound of {limit}")
    return None


class _Rendered:
    """What ``legacy_import._prove_round_trip`` reads of a capture: the vault and the manifest."""

    def __init__(self, root, manifest):
        self.root, self.manifest = Path(root), manifest


def _render_items(root, conn, manifest, cid):
    """Every item file of a markdown-items collection, unstamped, each proved to parse back to its row."""
    files = []
    for row_id, key, body, natural, view_path in conn.execute(
            "SELECT row_id,item_key,body,natural_key,view_path FROM items WHERE collection_id=? ORDER BY row_id",
            (cid,)):
        values = typed_storage.item_values(conn, row_id)
        legacy_import._prove_round_trip(_Rendered(root, manifest), key, values, body, natural)
        files.append((view_path, record_formats.render_markdown_item(manifest, values, key, body).encode("utf-8")))
    return files


def _render_log(root, conn, manifest, cid, frame_json):
    """The log file in the frame the import captured, as the legacy file held it, proved to parse to the rows."""
    frame = json.loads(frame_json)
    order = "DESC" if manifest.storage.descriptor.get("insertion") == "newest-first" else "ASC"
    rows = [(key, typed_storage.item_values(conn, row_id)) for row_id, key in conn.execute(
        f"SELECT row_id,item_key FROM items WHERE collection_id=? ORDER BY created_txn {order}, row_id {order}",
        (cid,)).fetchall()]
    data = (frame["text"] + "".join(record_formats.render_markdown_log_item(
        manifest, values, key, frame["newline"]) for key, values in rows) + frame["suffix"]).encode("utf-8")
    parsed = record_formats.MarkdownLogAdapter(Path(root), manifest).read_bytes(data).records
    if [(record.identity.key, collections.normalize_item_values(manifest.schema, record.values))
            for record in parsed] != rows:
        legacy_import._refuse("b", "the rendered log does not parse back to the store's rows")
    return [(manifest.storage.source, data)]


def _held(conn, manifest, cid):
    """Each held candidate's exact file bytes; one file mode cannot parse has no file form."""
    files = []
    for held_id, kind, view_path, data in conn.execute(
            "SELECT held_id,kind,view_path,held_bytes FROM held_candidates WHERE collection_id=? ORDER BY held_id",
            (cid,)):
        # A write refusal keeps its exact file bytes; the store's other held kinds have no file form.
        if kind != "write-refusal":
            raise collections.CollectionError(UNREPRESENTABLE, f"a held {kind} correction has no file form")
        records._parse_held_candidate_bytes(manifest, held_id, data)
        files.append((view_path, data))
    return files


def _disk_files(root, relative):
    """Every file below one vault directory, skipping Exomem's own registered scratch."""
    found, pending = [], [Path(root) / relative]
    while pending:
        try:
            entries = list(os.scandir(pending.pop()))
        except (FileNotFoundError, NotADirectoryError):
            continue
        for entry in entries:
            path = Path(entry.path).relative_to(root).as_posix()
            if vault._is_registered_internal_state_artifact(path):
                continue
            if entry.is_dir(follow_symlinks=False):
                pending.append(Path(entry.path))
            else:
                found.append(path)
    return found


def _view_edits(root, conn, manifest, cid, targets):
    """The views a person changed since the store published them, until P2 classifies such edits.

    Each view must hold the bytes the store published or left pending, or the export's
    own target (None: removed), and the collection's directories no file the store does
    not know, because file mode would read it as content.
    """
    known, edited = set(), []
    for path, kind, published, pending in conn.execute(
            "SELECT path,kind,published_sha256,pending_sha256 FROM projection_state WHERE collection_id=?", (cid,)):
        # nosemgrep: ep-word-membership -- the schema's closed projection kinds; see _RENDERED.
        if kind not in _RENDERED:
            raise collections.CollectionError(UNREPRESENTABLE, f"a {kind} view has no file form")
        known.add(path)
        data = _read(root, path)
        found = None if data is None else _sha(data)
        # A view the store never installed is absent, not deleted.
        allowed = {published, pending, *([None] if published is None else []),
                   *([] if path not in targets else [None if targets[path] is None else _sha(targets[path])])}
        if found not in allowed:
            edited.append(path)
    directories = [records._held_directory(manifest)]
    if manifest.storage.strategy == "markdown-items":
        directories.append(manifest.storage.source)
    edited += [path for directory in directories for path in _disk_files(root, directory) if path not in known]
    return sorted(edited)


def _plan_collection(root, conn, entry, store_id, plan_id):
    """One routed collection's export row, and its work when the store wrote it.

    ``plan_id`` is the export's identity once its intent exists: the checkpointed manifest
    then counts as a target too, so a resumed run plans the same work.
    """
    cid = entry["collection_id"]
    manifest_path, layout, frame_json, head = conn.execute(
        "SELECT manifest_path,layout,log_frame_json,audit_head FROM collections WHERE collection_id=?",
        (cid,)).fetchone()
    text = conn.execute(
        "SELECT m.manifest_text FROM collection_manifests m JOIN collections c ON c.collection_id=m.collection_id "
        "AND c.manifest_version=m.manifest_version WHERE c.collection_id=?", (cid,)).fetchone()[0]
    manifest = collections.parse_manifest_bytes(Path(root), manifest_path, text.encode("utf-8"))
    imported = conn.execute(
        "SELECT txn_id FROM txns WHERE collection_id=? AND operation='legacy_import' AND legacy_event_json IS NULL",
        (cid,)).fetchone()
    first, last, native = conn.execute(
        "SELECT MIN(commit_seq),MAX(commit_seq),COUNT(*) FROM txns WHERE collection_id=? "
        "AND operation!='legacy_import'", (cid,)).fetchone()
    row = {"collection_id": cid, "path": manifest_path, "layout": layout,
           "rows": conn.execute("SELECT COUNT(*) FROM items WHERE collection_id=?", (cid,)).fetchone()[0]}
    rendered, targets, resolved = None, {}, {}
    try:
        if imported is not None and native:
            rendered = {"manifest": text.encode("utf-8"), "sources": (
                _render_items(root, conn, manifest, cid) if layout == "markdown-items"
                else _render_log(root, conn, manifest, cid, frame_json)), "held": _held(conn, manifest, cid)}
            held = {path for path, _ in rendered["held"]}
            # A held file the store resolved is removed, guarded by the bytes the store published.
            resolved = {path: sha for path, sha in conn.execute(
                "SELECT path,published_sha256 FROM projection_state WHERE collection_id=? AND kind='held'", (cid,))
                if path not in held and sha is not None}
            targets = {manifest_path: rendered["manifest"], **dict(rendered["sources"]), **dict(rendered["held"]),
                       **dict.fromkeys(resolved)}
        blocker = _unexportable(manifest, imported, rendered)
        if blocker is None:
            allowed = dict(targets)
            if plan_id is not None and rendered is not None:
                allowed[manifest_path] = _checkpointed(text, manifest, _transition(plan_id, cid)).encode("utf-8")
            edited = _view_edits(root, conn, manifest, cid, allowed)
            if edited:
                blocker = _blocker(VIEW_EDITED, "these views changed since the store published them: "
                                   + ", ".join(edited) + "; restore their bytes before exporting")
    except (collections.CollectionError, legacy_import.LegacyProofError, ValueError) as error:
        code = getattr(error, "code", type(error).__name__)
        blocker = _blocker(OVER_BOUND if code == _LOG_BOUND else UNREPRESENTABLE, f"{code}: {error}")
    if blocker is not None:
        return {**row, "status": "blocked", "blocker": blocker}, None
    if rendered is None:
        return {**row, "status": "unwritten"}, None
    basis = sorted((path, None if (data := _read(root, path)) is None else _sha(data),
                    None if target is None else _sha(target)) for path, target in targets.items())
    imported_text = conn.execute("SELECT manifest_text FROM collection_manifests WHERE collection_id=? "
                                 "AND manifest_version=1", (cid,)).fetchone()[0]
    work = {"manifest_path": manifest_path, "manifest_text": text, "targets": targets, "resolved": resolved,
            "basis": basis,
            # The checkpoint continues the legacy chain from the head the store imported.
            "parent_id": collections.parse_manifest_bytes(
                Path(root), manifest_path, imported_text.encode("utf-8")).audit_head or "baseline",
            "gap_code": _HISTORY_CODE.format(store_id=store_id, first=first, last=last), "collection_head": head}
    return {**row, "status": "written", "files": sum(1 for _, before, after in basis if before != after)}, work


def plan(root, conn, *, plan_id=None):
    """The sealed export plan of the store ``conn`` reads, and the work of each written collection."""
    head = chain.verify_store_chain(conn)
    store_id = conn.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    raw = authority.read_marker(root)
    marker = authority.parse_marker(root, raw)
    if marker["store_id"] != store_id:
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "the marker routes to another store")
    rows, work = [], {}
    for entry in sorted(marker["collections"], key=lambda item: item["manifest_path"]):
        row, planned = _plan_collection(root, conn, entry, store_id, plan_id)
        rows.append(row)
        if planned is not None:
            work[entry["collection_id"]] = planned
    verdict = "blocked" if any(row["status"] == "blocked" for row in rows) else "exportable"
    basis = {"store_id": store_id, "head": list(head), "marker_sha256": _sha(raw),
             "collections": [[row["collection_id"], row["status"], work.get(row["collection_id"], {}).get("basis")]
                             for row in rows]}
    sealed = {"mode": "collections-store-export", "verdict": verdict, "store_id": store_id,
              "head": {"commit_seq": head[0], "head_hash": head[1]}, "collections": rows}
    if verdict == "exportable":
        sealed["plan_id"] = _sha(_PLAN_DOMAIN + records._canonical_json(basis))[:32]
    return sealed, work


def _checkpointed(text, manifest, transition_id):
    """The manifest the checkpoint publishes: the store's text, its audit head the checkpoint."""
    return record_formats.render_manifest_audit_head(
        text, transition_id, semantic_profile=manifest.semantic_profile, reader_version=2)


def _transition(plan_id, cid):
    return _sha(_TRANSITION_DOMAIN + records._canonical_json([plan_id, cid]))[:24]


def _local_intent(root):
    """The live store's export intent, or None; another unfinished intent refuses."""
    path = connection.store_path(root)
    if not path.exists():
        return None
    with closing(connection.open_reader(path)) as reader:
        intent = authority.pending_create(reader)
    if intent is not None and intent["kind"] == authority.MIGRATE:
        raise admission.migration_pending()
    if intent is not None and intent["kind"] != authority.EXPORT:
        raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT",
                                   "a collection create is still pending; finish it before exporting")
    return intent


def _report(step, verdict, **found):
    return {"mode": "collections-store-export", "step": step, "verdict": verdict, **found}


def preview(vault_root) -> dict:
    """The read-only export plan with its ``plan_id``; the vault, the store and the lease stay untouched.

    An unfinished export previews its intent instead, so the owner resumes it with that identity.
    """
    root = Path(vault_root).resolve()
    intent = _local_intent(root)
    if intent is not None:
        return _report("preview", "export_pending", plan_id=intent["plan_id"], exported_at=intent["exported_at"],
                       collections=[{"collection_id": entry["collection_id"], "path": entry["manifest_path"]}
                                    for entry in intent["collections"]])
    if authority.read_marker(root) is None:
        return _report("preview", "nothing_to_export", **(
            {"local_store": "this host keeps a store the vault no longer routes to; retire it with "
                            "`exomem collections export --retire-local`"}
            if connection.store_path(root).exists() else {}))
    if not connection.store_path(root).exists():
        raise CollectionStoreError(takeover.SYNC_PENDING, "this vault routes collections to a store not opened "
                                   "here; start the service once so it adopts the replica, then export")
    recorded = admission._coordinator_head()
    with closing(connection.open_reader(connection.store_path(root))) as reader:
        sealed, _ = plan(root, reader)
        # Exporting a store behind the coordinator's head would lose another host's writes.
        # nosemgrep: ep-word-membership -- chain.head_relation returns these closed relation tokens.
        if recorded not in (None, owner.UNKNOWN) and (
                recorded.store_id != sealed["store_id"]
                or chain.head_relation(reader, recorded.commit_seq, recorded.head_hash) not in ("same", "ancestor")):
            raise CollectionStoreError(takeover.SYNC_PENDING, "the coordinator's recorded head is not in this "
                                       "store; start the service once so it catches up, then export")
    return {**sealed, "step": "preview"}


def _refused(sealed):
    blocked = [f"{row['path']} ({row['blocker']['code']}: {row['blocker']['reason']})"
               for row in sealed["collections"] if row["status"] == "blocked"]
    return CollectionStoreError(BLOCKED, "these collections cannot leave the store, so the vault stays in it: "
                                + "; ".join(blocked))


def _stale():
    return CollectionStoreError(STALE, "the store, the marker or a view changed since the preview; preview again")


def _require_retire_route(fence_client):
    """Refuse before any change unless the coordinator serves the fence retire the export ends with.

    Without this check, an export would write every file and then stop with its
    collections unavailable until the coordinator is upgraded or the credential fixed.
    A false refusal costs the owner one coordinator check before any file changes.
    """
    try:
        fence_client.probe_collection_store_fence_retire()
    except OpError as error:
        if error.code != "WRITER_COORDINATOR_CONTRACT_ABSENT":
            raise
        raise CollectionStoreError(
            preflight.UPGRADE_REQUIRED, "the writer-lease coordinator cannot retire a collection store fence; "
            f"upgrade it to Exomem {preflight._running_release()}, the release running here") from error


def _begin(session, manager, fence_client, *, plan_id, why):
    """Record the export intent on an admitted store whose plan is still the previewed one."""
    records._validate_why(why)
    opened = admission.open_store(session, manager, fence_client=fence_client)
    if opened["status"] != "admitted":
        raise session.runtime(retire=False)._refusal()
    runtime = session.runtime()
    with manager.writer_authority_guard(vault_root=session.root):
        token = manager._fencing_token
        raw = authority.read_marker(session.root)
        with closing(connection.open_reader(session.path)) as reader:
            sealed, _ = plan(session.root, reader)
        if sealed["verdict"] != "exportable":
            raise _refused(sealed)
        if sealed["plan_id"] != plan_id:
            raise _stale()
        marker = authority.parse_marker(session.root, raw)
        _require_retire_route(fence_client)
        intent = {"version": 2, "kind": authority.EXPORT, "request_id": "export:" + uuid.uuid4().hex,
                  "plan_id": plan_id, "expected_marker": raw.decode("utf-8"), "why": why,
                  "exported_at": dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ"),
                  "collections": [{name: entry[name] for name in ("collection_id", "manifest_path", "source_path")}
                                  for entry in marker["collections"]]}
        if not runtime.retire_idle_handle():
            raise CollectionStoreError(takeover.BUSY, "the live store is still borrowed")
        with session.writer(token) as writer, writer.handle.transaction() as conn:
            session.require(token)
            if authority.pending_create(conn) is not None:
                raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "another intent owns this store")
            conn.execute("INSERT INTO store_meta(key,value) VALUES (?,?)", (
                authority.PENDING_CREATE, json.dumps(intent, sort_keys=True, separators=(",", ":"))))
    return intent


def _renew(session, token):
    """Renew the store-bound lease between steps, as replica publication does: no renewer runs offline."""
    if session.manager._collection_store is not None:
        session.manager._renew_collection_store(token, due_only=True)
    session.require(token)


@contextmanager
def _files_as(session, token):
    """Write vault files as this offline owner: its manager is the active one, under its writer fence."""
    manager_context = writer_lease._ACTIVE_LEASE_MANAGER.set(session.manager)
    fence_context = writer_lease._ACTIVE_WRITE_FENCE.set((session.manager, token))
    try:
        yield
    finally:
        writer_lease._ACTIVE_WRITE_FENCE.reset(fence_context)
        writer_lease._ACTIVE_LEASE_MANAGER.reset(manager_context)


def _held_file(session, relative):
    """The held directory and file result for one vault-relative path, through the session's custody."""
    parent, leaf = relative.rsplit("/", 1)
    return session.fs.parent((session.root / parent).relative_to(session.root.parent).as_posix(),
                             access="mutate"), leaf


def _remove(session, relative, sha):
    """Unlink a held file the store resolved, only while it holds the bytes the store published."""
    directory, leaf = _held_file(session, relative)
    if directory.error is not None and directory.error.code == "MISSING":
        return
    with directory.require() as parent:
        found = session.fs.file(parent, leaf, access="mutate")
        if found.error is not None and found.error.code == "MISSING":
            return
        with found.require() as held:
            if _sha(session.fs.read(held).require()) != sha:
                raise _stale()
            session.fs.unlink(held).require()
        session.fs.flush_directory(parent).require()


def _write_collection(session, token, intent, cid, work, store_id, head, deferred):
    """Render one collection back to files, then publish its checkpoint; a rerun converges.

    The plan has already checked every file: each holds its published view or its target.
    The index fan-out of what it writes lands in ``deferred``: the graph rebuild it waits
    on needs the boundary the export holds, so the export runs it after releasing that.
    """
    root = session.root
    checkpointed = _checkpointed(work["manifest_text"], collections.parse_manifest_bytes(
        root, work["manifest_path"], work["manifest_text"].encode("utf-8")), _transition(intent["plan_id"], cid))
    writes = []
    for path, target in work["targets"].items():
        current = _read(root, path)
        if current == target or target is None or path == work["manifest_path"] and current == checkpointed.encode(
                "utf-8"):
            continue
        writes.append(vault.PlannedWrite(Path(root) / path, target.decode("utf-8"), create_only=current is None,
                                         expected_hash=None if current is None else _sha(current),
                                         ensure_directories=(Path(root, path).parent,)))
    with _files_as(session, token):
        if writes:
            intents = []
            deferred.append((vault.batch_atomic_write(writes, vault_root=root, post_commit_fanout=False,
                                                      publication_intents_out=intents), intents))
        for path, sha in work["resolved"].items():
            _remove(session, path, sha)
        _renew(session, token)
        records.export_checkpoint(
            root, work["manifest_path"], manifest_text=work["manifest_text"],
            transition_id=_transition(intent["plan_id"], cid), parent_id=work["parent_id"],
            gap_code=work["gap_code"], why=intent["why"], deferred=deferred,
            payload_hash=records.export_checkpoint_payload_hash(
                store_id=store_id, collection_head=work["collection_head"], store_head=head))


def _require_reads_as_served(root, conn, intent, work):
    """Prove file mode reads every routed collection as the store served it, and each checkpoint holds.

    A written collection must read ``acknowledged_gap`` with its checkpoint as the
    discontinuity; a legacy gap or incomplete history the import carried may remain.
    """
    verified = []
    for entry in intent["collections"]:
        cid = entry["collection_id"]
        try:
            manifest = collections.load_manifest(root, Path(root) / entry["manifest_path"])
            snapshot = record_formats.load_adapter(root, manifest).read()
        except collections.CollectionError as error:
            raise CollectionStoreError(UNVERIFIED, f"file mode cannot read {entry['manifest_path']}: {error}") \
                from error
        stored = {key: (typed_storage.item_values(conn, row_id), body, payload) for row_id, key, body, payload
                  in conn.execute("SELECT row_id,item_key,body,payload_hash FROM items WHERE collection_id=?", (cid,))}
        found = {record.identity.key: record for record in snapshot.records}
        if set(found) != set(stored) or any(record.ambiguous for record in snapshot.records) or any(
                stored[key] != (collections.normalize_item_values(manifest.schema, record.values),
                                legacy_import._body(manifest, stored[key][0], record.body),
                                records._payload_hash(manifest, key, record.values, record.body))
                for key, record in found.items()):
            raise CollectionStoreError(UNVERIFIED, f"{entry['manifest_path']} does not read as the store served it")
        audit = records.inspect_audit_gap(root, manifest)
        if cid in work:
            (legacy,) = conn.execute("SELECT legacy_audit_status FROM collections WHERE collection_id=?",
                                     (cid,)).fetchone()
            # nosemgrep: ep-word-membership -- the inspector's closed audit statuses.
            if not (audit["status"] == "acknowledged_gap" or audit["status"] == legacy
                    and legacy in ("gap", "history_incomplete")) or audit["status"] != "history_incomplete" and (
                    audit.get("discontinuity", {}).get("checkpoint_transition") != _transition(intent["plan_id"], cid)):
                raise CollectionStoreError(UNVERIFIED, f"{entry['manifest_path']} reads audit {audit['status']} "
                                           "without the export's checkpoint")
        verified.append({"collection_id": cid, "path": entry["manifest_path"],
                         "status": "written" if cid in work else "unwritten", "audit": audit["status"]})
    return verified


def _detach(session, manager):
    """Unbind the store runtime from the lease once the fence is retired, before the store is tombstoned.

    The lease then needs no store capability, and releasing it must not flush a replica
    of a store that is leaving.
    """
    from .runtime import _SERVING

    runtime = manager._collection_store
    if runtime is None:
        return
    if not runtime.retire_idle_handle():
        raise CollectionStoreError(takeover.BUSY, "the live store is still borrowed")
    with manager._lock:
        manager._collection_store = None
    runtime._admitted_token = None
    if _SERVING.get(session.root) is runtime:
        _SERVING.pop(session.root)


def _rename(fs, directory, leaf, tombstone, *, expected=None):
    """Rename one leaf to its tombstone, once; ``expected`` bytes compare first. Whether a leaf existed."""
    found = fs.file(directory, leaf, access="mutate")
    if found.error is not None and found.error.code == "MISSING":
        return False
    with found.require() as current:
        if expected is not None and fs.read(current).require() != expected:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "the marker changed during the export")
        fs.rename(current, directory, tombstone, replace=False).require()
    fs.flush_directory(directory).require()
    return True


def _retire_marker(session, token, intent):
    """Rename the routing marker this export retires, never writing a mode released launchers reject.

    A rerun finds it already renamed.
    """
    session.require(token)
    tombstone = MARKER_LEAF + EXPORTED + intent["exported_at"]
    if not _rename(session.fs, session.namespace, MARKER_LEAF, tombstone,
                   expected=intent["expected_marker"].encode("utf-8")):
        found = session.fs.file(session.namespace, tombstone)
        if found.error is not None:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "the routing marker is gone")
        with found.require():
            pass
    return tombstone


def _tombstone_store(session, at):
    """Fold the live store into one file and rename it, with any derived rows beside it, as the final step."""
    path = session.path
    if path.exists():
        try:
            with closing(sqlite3.connect(path, isolation_level=None, timeout=0)) as live:
                if live.execute("PRAGMA journal_mode=DELETE").fetchone() != ("delete",):
                    raise sqlite3.OperationalError("journal mode unchanged")
        except sqlite3.OperationalError as error:
            raise CollectionStoreError(takeover.BUSY, "the live store is still open") from error
    if any(path.with_name(path.name + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise CollectionStoreError(takeover.BUSY, "the live store is still open")
    fs = session.state_fs
    with fs.parent(path.parent.name, access="mutate").require() as directory:
        # Derived rows rebuild from the store's import log, so they follow it; the store goes last.
        for name in (derived_rows.FILENAME, connection.STORE_FILENAME):
            _rename(fs, directory, name, name + EXPORTED + at)


def apply(session, manager, fence_client, *, plan_id, why) -> dict:
    """Export inside the owner's offline session, or resume the export this store's intent records.

    Order: files and checkpoints, their verification, the fence retire, then the
    marker, the replica, the descriptors and the store. The fence is retired only after
    every collection is verified. A rerun after the retire verifies again while the
    marker still routes, then finishes the renames; once the marker is renamed, a rerun
    only finishes the rest.
    """
    root = session.root
    session.verify_custody()
    admission._ProducerSession.bind(session, manager)
    admission._bind_fence_client(session, fence_client)
    deferred = []
    with manager.consistency_guard(root, operation="collection_store_export"):
        intent = _local_intent(root)
        resumed = intent is not None
        if intent is None:
            if authority.read_marker(root) is None:
                return preview(root) | {"step": "apply"}
            intent = _begin(session, manager, fence_client, plan_id=plan_id, why=why)
        elif intent["plan_id"] != plan_id:
            raise _stale()
        marker = authority.parse_marker(root, intent["expected_marker"])
        store_id, generation = marker["store_id"], marker["collection_store_fence"]["generation"]
        fence = fence_client.collection_store_fence()
        if fence.enrolled:
            admission._require_fence(fence, marker)
            # Its capability holds the lease while the fence stands.
            runtime = session.runtime()
            with manager.writer_authority_guard(vault_root=root):
                token = manager._fencing_token
                with session.writer(token) as writer:
                    if resumed:
                        # Until the replica carries the intent, another host may adopt it and write,
                        # so a resume first proves the coordinator's head is this store's. This
                        # run's own _begin admitted the store under this token, which proved it.
                        admission._verify_acquisition(writer, runtime, fence, marker)
                    # The replica carries the intent before any file changes, so no host adopts it.
                    published = admission._publish_current_epoch(session, writer, token)
                if published.status != "published":
                    raise CollectionStoreError("COLLECTION_STORE_FLUSH_PENDING",
                                               f"replica publication: {published.reason or published.status}")
                with closing(connection.open_reader(session.path)) as reader:
                    head = chain.verify_store_chain(reader)
                    sealed, work = plan(root, reader, plan_id=intent["plan_id"])
                    if sealed["verdict"] != "exportable":
                        raise _refused(sealed)
                    for cid, planned in work.items():
                        _renew(session, token)
                        _write_collection(session, token, intent, cid, planned, store_id, head, deferred)
                    _renew(session, token)
                    verified = _require_reads_as_served(root, reader, intent, work)
                _renew(session, token)
                fence_client.retire_collection_store_fence(expected_generation=generation, store_id=store_id)
            _detach(session, manager)
        elif fence.generation != generation:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "the coordinator fences another generation")
        elif authority.read_marker(root) == intent["expected_marker"].encode("utf-8"):
            # A retired fence does not prove this export wrote the files: a hand retire leaves
            # none. The live store goes last, so while the marker routes it still verifies them.
            with closing(connection.open_reader(session.path)) as reader:
                sealed, work = plan(root, reader, plan_id=intent["plan_id"])
                if sealed["verdict"] != "exportable":
                    raise _refused(sealed)
                verified = _require_reads_as_served(root, reader, intent, work)
        else:
            verified = None
        with manager.writer_authority_guard(vault_root=root):
            token = manager._fencing_token
            tombstone = _retire_marker(session, token, intent)
            session.require(token)
            _rename(session.fs, session.namespace, connection.STORE_FILENAME,
                    connection.STORE_FILENAME + EXPORTED + intent["exported_at"])
            state_migration.retire_collection_store_compatibility(root, authority_check=lambda: session.require(token))
            session.require(token)
            _tombstone_store(session, intent["exported_at"])
    for written, intents in deferred:
        vault.post_commit_batch_fanout(root, written, None, None, publication_intents=intents)
    return _report("apply", "exported", status="exported", plan_id=intent["plan_id"], store_id=store_id,
                   exported_at=intent["exported_at"], retired_fence_generation=generation,
                   marker=f"{authority.marker_path(root).parent.relative_to(root).as_posix()}/{tombstone}",
                   **({"collections": verified} if verified is not None else {"resumed": "after the marker retire"}))


def retire_local(session, manager, fence_client) -> dict:
    """Once a host's vault copy routes nothing to a store: tombstone its stale store and retire its descriptors.

    It refuses while the vault still routes collections here, and touches a local store
    only when an exported marker in the vault names that store and the coordinator no
    longer fences it, so a store the export did not retire stays put. A host without a
    store only retires the descriptors.
    """
    root = session.root
    session.verify_custody()
    admission._ProducerSession.bind(session, manager)
    admission._bind_fence_client(session, fence_client)
    with manager.consistency_guard(root, operation="collection_store_export_retire_local"):
        if authority.read_marker(root) is not None:
            raise CollectionStoreError(takeover.SYNC_PENDING, "this vault still routes collections to its store; "
                                       "wait until the exported vault reaches this host")
        exported = {}
        for path in authority.marker_path(root).parent.glob(MARKER_LEAF + EXPORTED + "*"):
            exported[authority.parse_marker(root, path.read_bytes())["store_id"]] = path.name[len(
                MARKER_LEAF + EXPORTED):]
        if not session.path.exists():
            # No store is left to tombstone, but the descriptors still keep an older release out.
            with manager.writer_authority_guard(vault_root=root):
                token = manager._fencing_token
                state_migration.retire_collection_store_compatibility(
                    root, authority_check=lambda: session.require(token))
            return _report("retire-local", "retired", status="retired", store="absent")
        with closing(connection.open_reader(session.path)) as reader:
            store_id = reader.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
            intent = authority.pending_create(reader)
        if intent is not None and intent["kind"] == authority.EXPORT:
            raise admission.export_pending()
        fence = fence_client.collection_store_fence()
        if store_id not in exported or fence.enrolled and fence.store_id == store_id:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "no finished export retired this host's "
                                       "store, so it stays; export the vault first")
        with manager.writer_authority_guard(vault_root=root):
            token = manager._fencing_token
            state_migration.retire_collection_store_compatibility(root, authority_check=lambda: session.require(token))
            session.require(token)
            _tombstone_store(session, exported[store_id])
    return _report("retire-local", "retired", status="retired", store_id=store_id, exported_at=exported[store_id])
