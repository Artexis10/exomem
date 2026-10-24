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

from .. import held_fs, record_formats, records, state_migration, vault, writer_lease
from .. import structured_collections as collections
from ..cli_ops import OpError
from . import (
    admission,
    authority,
    chain,
    connection,
    derived_rows,
    history,
    legacy_import,
    legacy_originals,
    log_view,
    owner,
    ownership,
    preflight,
    schema,
    takeover,
    typed_storage,
    views,
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
    with views.presentation_resolver(conn, root, manifest) as resolver:
        for row_id, key, body, natural, view_path in conn.execute(
                "SELECT row_id,item_key,body,natural_key,view_path FROM items WHERE collection_id=? ORDER BY row_id",
                (cid,)):
            values = typed_storage.item_values(conn, row_id)
            rendered = legacy_import._prove_round_trip(
                _Rendered(root, manifest), key, values, body, natural, resolve_relationship=resolver
            )
            files.append((view_path, rendered.encode("utf-8")))
    return files


def _render_log(root, conn, manifest, cid, frame_json):
    """The log file in the frame the import captured, as the legacy file held it, proved to parse to the rows.

    It is the store's log view without its block stamps, which file mode has no store to name.
    """
    blocks = log_view.rows(conn, manifest)
    data = log_view.render(manifest, log_view.load(frame_json, manifest), blocks).encode("utf-8")
    parsed = record_formats.MarkdownLogAdapter(Path(root), manifest).read_bytes(data).records
    if [(record.identity.key, record.values)
            for record in parsed] != [(row["item_key"], row["values"]) for row in blocks]:
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
        if kind != "history" and kind not in _RENDERED:
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
    history_main = history.main_path(manifest)
    history_directory = str(PurePosixPath(history.page_path(manifest, 1)).parent)
    directories.append(history_directory)
    if os.path.lexists(Path(root) / history_main) and history_main not in known:
        edited.append(history_main)
    if os.path.lexists(Path(root) / history_directory) and not (Path(root) / history_directory).is_dir():
        edited.append(history_directory)
    edited += [path for directory in directories for path in _disk_files(root, directory) if path not in known]
    return sorted(edited)


def _plan_collection(root, conn, entry, store_id, plan_id, parents, *, views=True):
    """One routed collection's export row, and its work when the store wrote it.

    ``plan_id`` is the export's identity once its intent exists: the checkpointed manifest
    then counts as a target too, so a resumed run plans the same work. ``parents`` holds
    the checkpoint an abandoned export left per collection, which the next checkpoint
    continues. ``views=False`` plans the work without checking the views against it,
    for an abandon that records which views hold it.
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
        elif imported is not None:
            # Projection stamping changes bytes, never the unwritten collection's rollback promise.
            targets = {manifest_path: text.encode("utf-8"), **legacy_originals.export_sources(root, conn, cid),
                       **{path: raw for path, raw in conn.execute(
                           "SELECT view_path,held_bytes FROM held_candidates WHERE collection_id=?", (cid,))}}
        # File mode cannot reconstruct history's per-effect release decisions; retire only registered exact bytes.
        retired = {path: None if (data := _read(root, path)) is None else _sha(data)
                   for (path,) in conn.execute("SELECT path FROM projection_state WHERE collection_id=? AND kind='history'",
                                              (cid,))}
        resolved.update(retired)
        targets.update(dict.fromkeys(retired))
        blocker = _unexportable(manifest, imported, rendered)
        if blocker is None and views:
            allowed = dict(targets)
            if plan_id is not None and rendered is not None:
                allowed[manifest_path] = _checkpointed(text, manifest, _transition(plan_id, cid)).encode("utf-8")
            edited = _view_edits(root, conn, manifest, cid, allowed)
            if edited:
                blocker = _blocker(VIEW_EDITED, "these views changed since the store published them: "
                                   + ", ".join(edited) + "; restore their bytes before exporting")
    except (collections.CollectionError, CollectionStoreError, ValueError) as error:
        code = getattr(error, "code", type(error).__name__)
        blocker = _blocker(OVER_BOUND if code == _LOG_BOUND else UNREPRESENTABLE, f"{code}: {error}")
    if blocker is not None:
        return {**row, "status": "blocked", "blocker": blocker}, None
    if rendered is None and not targets:
        return {**row, "status": "unwritten"}, None
    basis = sorted((path, None if (data := _read(root, path)) is None else _sha(data),
                    None if target is None else _sha(target)) for path, target in targets.items())
    imported_text = conn.execute("SELECT manifest_text FROM collection_manifests WHERE collection_id=? "
                                 "AND manifest_version=1", (cid,)).fetchone()[0]
    work = {"manifest_path": manifest_path, "manifest_text": text, "targets": targets, "resolved": resolved,
            "checkpoint": rendered is not None,
            "basis": basis,
            # The checkpoint continues the legacy chain from the head the store imported, or from
            # the checkpoint an abandoned export left: another parent would fork that chain.
            "parent_id": parents.get(cid) or collections.parse_manifest_bytes(
                Path(root), manifest_path, imported_text.encode("utf-8")).audit_head or "baseline",
            "gap_code": _HISTORY_CODE.format(store_id=store_id, first=first, last=last), "collection_head": head}
    if rendered is None:
        return {**row, "status": "unwritten"}, work
    return {**row, "status": "written",
            "files": sum(1 for _, before, after in basis if before != after)}, work


def _parents(conn):
    """Each collection's checkpoint that an abandoned export left in the files, by collection id."""
    row = conn.execute("SELECT value FROM store_meta WHERE key=?", (schema.META_EXPORT_PARENTS,)).fetchone()
    return {} if row is None else json.loads(row[0])


def plan(root, conn, *, plan_id=None, views=True):
    """The sealed export plan of the store ``conn`` reads, and the work of each written collection."""
    head = chain.verify_store_chain(conn)
    store_id = conn.execute("SELECT value FROM store_meta WHERE key='store_id'").fetchone()[0]
    raw = authority.read_marker(root)
    marker, _ = ownership.declarations(root, conn)
    if marker is None:
        raise CollectionStoreError(BLOCKED, "the store has no canonical collections")
    authority.require_projection_ownership(conn, marker, root=root)
    if raw is not None:
        hint = authority.parse_marker(root, raw)
        for entry in hint["collections"]:
            authority.require_selected(conn, hint, entry, root=root)
    rows, work, parents = [], {}, _parents(conn)
    for entry in sorted(marker["collections"], key=lambda item: item["manifest_path"]):
        row, planned = _plan_collection(root, conn, entry, store_id, plan_id, parents, views=views)
        rows.append(row)
        if planned is not None:
            work[entry["collection_id"]] = planned
    verdict = "blocked" if any(row["status"] == "blocked" for row in rows) else "exportable"
    basis = {"store_id": store_id, "head": list(head), "marker_sha256": None if raw is None else _sha(raw),
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
    if ownership.current(root).marker is None:
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


def _require_plan(sealed, plan_id):
    if sealed["verdict"] != "exportable":
        raise _refused(sealed)
    if sealed["plan_id"] != plan_id:
        raise _stale()


def _require_retire_route(fence_client):
    """Refuse before any change unless the coordinator serves the fence retire the export ends with.

    Without this check, an export would write every file and then stop with its
    collections unavailable until the coordinator is upgraded or the credential fixed.
    The coordinator must also read a retired fence back, which a rerun after the retire
    checks before it goes forward. A false refusal costs the owner one coordinator check
    before any file changes.
    """
    try:
        fence_client.probe_collection_store_fence_retire()
        fence_client.retired_collection_store_fence()
    except OpError as error:
        if error.code != "WRITER_COORDINATOR_CONTRACT_ABSENT":
            raise
        raise CollectionStoreError(
            preflight.UPGRADE_REQUIRED, "the writer-lease coordinator cannot retire a collection store fence or read a retired one; "
            f"upgrade it to Exomem {preflight._running_release()}, the release running here") from error


def _begin(session, manager, fence_client, *, plan_id, why):
    """Record the export intent on an admitted store whose plan is still the previewed one; the intent and token."""
    records._validate_why(why)
    # Admission publishes its lease epoch. Reject a known bad local plan before it changes the replica.
    if session.path.exists():
        with closing(connection.open_reader(session.path)) as reader:
            _require_plan(plan(session.root, reader)[0], plan_id)
    _require_retire_route(fence_client)
    opened = admission.open_store(session, manager, fence_client=fence_client)
    if opened["status"] != "admitted":
        raise session.runtime(retire=False)._refusal()
    runtime = session.runtime()
    with manager.writer_authority_guard(vault_root=session.root):
        token = manager._fencing_token
        raw = authority.read_marker(session.root)
        with closing(connection.open_reader(session.path)) as reader:
            sealed, _ = plan(session.root, reader)
            marker = ownership.current(session.root, conn=reader).marker
        _require_plan(sealed, plan_id)
        _require_retire_route(fence_client)
        if marker is None:
            raise CollectionStoreError(BLOCKED, "the store has no admitted collections")
        hint = None if raw is None else authority.parse_marker(session.root, raw)
        if hint is None or authority.intent_collection_ids(hint) != authority.intent_collection_ids(marker):
            if hint is not None:
                marker = {**marker, "authority_epoch": hint["authority_epoch"],
                          "collection_store_fence": hint["collection_store_fence"]}
            repaired = json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()
            # Repair only the validated compatibility preimage; canonical data and the plan stay unchanged.
            admission._replace_marker(session, token, raw, repaired)
            raw = repaired
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
    return intent, token


def _renew(session, token):
    """Renew the store-bound lease between steps of this synchronous offline export."""
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
        if work["checkpoint"]:
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
                stored[key] != (record.values,
                                legacy_import._body(manifest, stored[key][0], record.body),
                                records._payload_hash(manifest, key, record.values, record.body))
                for key, record in found.items()):
            raise CollectionStoreError(UNVERIFIED, f"{entry['manifest_path']} does not read as the store served it")
        audit = records.inspect_audit_gap(root, manifest)
        if cid in work and work[cid]["checkpoint"]:
            (legacy,) = conn.execute("SELECT legacy_audit_status FROM collections WHERE collection_id=?",
                                     (cid,)).fetchone()
            # nosemgrep: ep-word-membership -- the inspector's closed audit statuses.
            if not (audit["status"] == "acknowledged_gap" or audit["status"] == legacy
                    and legacy in ("gap", "history_incomplete")) or audit["status"] != "history_incomplete" and (
                    audit.get("discontinuity", {}).get("checkpoint_transition") != _transition(intent["plan_id"], cid)):
                raise CollectionStoreError(UNVERIFIED, f"{entry['manifest_path']} reads audit {audit['status']} "
                                           "without the export's checkpoint")
        elif cid in work and any(_read(root, path) != raw for path, raw in work[cid]["targets"].items()):
            raise CollectionStoreError(UNVERIFIED, "an unwritten collection differs from its imported bytes")
        verified.append({"collection_id": cid, "path": entry["manifest_path"],
                         "status": "written" if cid in work and work[cid]["checkpoint"] else "unwritten", "audit": audit["status"]})
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


def _write_and_verify(session, token, intent, store_id, deferred):
    """Write every collection's files and checkpoint from the frozen store, then prove file mode reads them back."""
    with closing(connection.open_reader(session.path)) as reader:
        head = chain.verify_store_chain(reader)
        sealed, work = plan(session.root, reader, plan_id=intent["plan_id"])
        if sealed["verdict"] != "exportable":
            raise _refused(sealed)
        for cid, planned in work.items():
            _renew(session, token)
            _write_collection(session, token, intent, cid, planned, store_id, head, deferred)
        if any(_read(session.root, path) is not None for planned in work.values()
               for path, target in planned["targets"].items() if target is None):
            raise CollectionStoreError(UNVERIFIED, "an export retirement target is still present")
        _renew(session, token)
        return _require_reads_as_served(session.root, reader, intent, work)


def _record_verified(session, runtime, token, intent):
    """Record in the intent that file mode reads every collection as the store served it, before any retire.

    A rerun after the fence retire then finishes the renames without reading the files
    again, which an edit made after the retire would fail.
    """
    if not runtime.retire_idle_handle():
        raise CollectionStoreError(takeover.BUSY, "the live store is still borrowed")
    with session.writer(token) as writer, writer.handle.transaction() as conn:
        session.require(token)
        if authority.pending_create(conn) != intent:
            raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "the export intent changed")
        intent["verified"] = True
        conn.execute("UPDATE store_meta SET value=? WHERE key=?", (
            json.dumps(intent, sort_keys=True, separators=(",", ":")), authority.PENDING_CREATE))


def _require_retired_head(fence_client, reader, store_id, generation):
    """Refuse to go forward from a retired fence unless the coordinator retired this store at its own head.

    Before the retire, another host may have adopted the replica and written rows that this
    store lacks; the files would then lose them. A false refusal keeps the vault as it is,
    still routed and with its fence retired, until that host's store reaches this host.
    """
    retired = fence_client.retired_collection_store_fence()
    if (retired.store_id, retired.generation) != (store_id, generation):
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "the coordinator retired another store fence")
    head = retired.collection_store_head
    (instance,) = reader.execute("SELECT value FROM store_meta WHERE key=?", (schema.META_INSTANCE_ID,)).fetchone()
    if head is not None and not admission.holds_head(reader, head, store_id, instance):
        raise CollectionStoreError(takeover.SYNC_PENDING, (
            "the head the coordinator recorded when it retired this store's fence is not in this store, so "
            "another host wrote rows that the files would lose; keep the services stopped and keep both stores, "
            "the marker and the fence as they are"))
    return retired


def completed_retirement_binding(root, fence_client, store_id=None):
    """Prove completed file authority from the paired export and retired head.

    A false acceptance can expose stale views; an unresolved proof delays relevant
    reads and local cleanup. Marker names alone never establish retirement.
    """
    from ..vault_portability import PortabilityError, _checked_collection_replica_structure

    try:
        if authority.read_marker(root) is not None:
            return None
        fence = fence_client.collection_store_fence()
        if fence.enrolled:
            return None
        if store_id is None:
            store_id = fence_client.retired_collection_store_fence().store_id
            if store_id is None:
                return None
        directory = authority.marker_path(root).parent
        for path in sorted(directory.glob(MARKER_LEAF + EXPORTED + "*")):
            with held_fs.acquire(root).require() as fs, \
                    fs.parent(directory.relative_to(root).as_posix()).require() as parent, \
                    fs.file(parent, path.name).require() as file:
                raw = fs.read(file).require()
            marker = authority.parse_marker(root, raw)
            generation = marker["collection_store_fence"]["generation"]
            if marker["store_id"] != store_id or generation != fence.generation:
                continue
            at = path.name[len(MARKER_LEAF + EXPORTED):]
            source = directory / (connection.STORE_FILENAME + EXPORTED + at)
            with _checked_collection_replica_structure(root, source_path=source) as verified:
                reader = verified.connection
                intent = authority.pending_create(reader)
                canonical, _ = ownership.declarations(root, reader, generation=generation)
                if (intent is None or intent["kind"] != authority.EXPORT or intent["exported_at"] != at
                        or intent["expected_marker"].encode() != raw or canonical["store_id"] != store_id
                        or authority.intent_collection_ids(intent) != {entry["collection_id"] for entry in canonical["collections"]}
                        or {entry["collection_id"] for entry in marker["collections"]}
                        != authority.intent_collection_ids(intent)):
                    raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "export retirement evidence differs")
                for entry in marker["collections"]:
                    authority.require_selected(reader, marker, entry, root=root)
                for item in intent["collections"]:
                    entry = authority.selected_entry(root, canonical, item["collection_id"])
                    if (entry["manifest_path"], entry["source_path"]) != (item["manifest_path"], item["source_path"]):
                        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "export collection binding differs")
                retired = _require_retired_head(fence_client, reader, store_id, generation)
                return at, (raw, verified.source_sha256, fence, retired)
        return None
    except (PortabilityError, held_fs.HeldFsError, OSError, sqlite3.Error, chain.StoreChainError,
            collections.CollectionError):
        raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "completed export evidence is unavailable") from None


def apply(session, manager, fence_client, *, plan_id, why) -> dict:
    """Export inside the owner's offline session, or resume the export this store's intent records.

    Order: files and checkpoints, their verification, then the fence retire, the
    marker, the replica, the descriptors and the store. The fence is retired only after
    every collection is verified, which the intent records first. A rerun after a retire
    goes on from the frozen store while the marker still routes, once the coordinator
    retired this store at its own head: after this export's retire it finishes the
    renames, and after a retire made by hand it first writes and verifies the files. Once
    the marker is renamed, a rerun only finishes the rest.
    """
    root = session.root
    session.verify_custody()
    admission._ProducerSession.bind(session, manager)
    admission._bind_fence_client(session, fence_client)
    deferred = []
    with manager.consistency_guard(root, operation="collection_store_export"):
        intent, begun = _local_intent(root), None
        if intent is None:
            if ownership.current(root).marker is None:
                return preview(root) | {"step": "apply"}
            intent, begun = _begin(session, manager, fence_client, plan_id=plan_id, why=why)
        elif intent["plan_id"] != plan_id:
            raise _stale()
        marker = authority.parse_marker(root, intent["expected_marker"])
        store_id, generation = marker["store_id"], marker["collection_store_fence"]["generation"]
        routed = authority.read_marker(root) == intent["expected_marker"].encode("utf-8")
        fence = fence_client.collection_store_fence()
        verified = None
        if fence.enrolled:
            admission._require_fence(fence, marker)
            # Its capability holds the lease while the fence stands.
            runtime = session.runtime()
            with manager.writer_authority_guard(vault_root=root):
                token = manager._fencing_token
                with session.writer(token) as writer:
                    if token != begun:
                        # Until the publication below copies the intent, the replica lacks it, so
                        # another host may adopt the replica and write once this run's lease lapses.
                        # A resume, or a run whose token changed since _begin, first proves the
                        # coordinator's head is this store's; under _begin's own token, _begin's
                        # admission of the store proved it.
                        admission._verify_acquisition(writer, runtime, fence, marker)
                    # The intent adds no commit; the publication copies the store because its intent
                    # changed, so a host that adopts the replica refuses it as exporting.
                    published = admission._publish_current_epoch(session, writer, token)
                if published.status != "published":
                    raise CollectionStoreError("COLLECTION_STORE_FLUSH_PENDING",
                                               f"replica publication: {published.reason or published.status}")
                verified = _write_and_verify(session, token, intent, store_id, deferred)
                _record_verified(session, runtime, token, intent)
                _renew(session, token)
                fence_client.retire_collection_store_fence(expected_generation=generation, store_id=store_id)
            _detach(session, manager)
        elif fence.generation != generation:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "the coordinator fences another generation")
        elif routed:
            # No enrolled fence admits a runtime and the marker cannot enrol again, so the store
            # is frozen: going forward from it loses nothing once its retired head is its own.
            with closing(connection.open_reader(session.path)) as reader:
                _require_retired_head(fence_client, reader, store_id, generation)
            if not intent.get("verified"):
                # A retire made by hand does not prove that this export wrote the files.
                runtime = session.runtime()
                with manager.writer_authority_guard(vault_root=root):
                    token = manager._fencing_token
                    verified = _write_and_verify(session, token, intent, store_id, deferred)
                    _record_verified(session, runtime, token, intent)
                _detach(session, manager)
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
                   **({"collections": verified} if verified is not None else {
                       "resumed": "after the fence retire" if routed else "after the marker retire"}))


def abandon(session, manager, fence_client, *, plan_id, why) -> dict:
    """Give up the unfinished export ``plan_id`` names while its fence stands, keeping every row (P1b.4a).

    For an export that refuses as unverified, or over a view a person edited, on a rerun.
    In one store transaction it records each file that holds the export's bytes as that
    view's published bytes, forgets each resolved held file the export removed, records
    each checkpoint the activity log holds as the parent of the next export's, and deletes
    the intent; then it publishes the replica and records the head. It rewrites no vault
    file and adopts no edit: the service's view-edit capture holds what a person changed.
    """
    records._validate_why(why)
    root = session.root
    session.verify_custody()
    admission._ProducerSession.bind(session, manager)
    admission._bind_fence_client(session, fence_client)
    with manager.consistency_guard(root, operation="collection_store_export_abandon"):
        intent = _local_intent(root)
        if intent is None:
            raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "no unfinished export owns this store")
        if intent["plan_id"] != plan_id:
            raise _stale()
        expected = intent["expected_marker"].encode("utf-8")
        marker = authority.parse_marker(root, expected)
        if authority.read_marker(root) != expected:
            # Past the marker retire only renames remain, and no host routes to the store any more.
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "the export already retired the marker; "
                                       "run `exomem collections export --plan-id ID --why REASON` to finish it")
        fence = fence_client.collection_store_fence()
        if not fence.enrolled:
            # Serving the store again would need its fence enrolled again, which no step does
            # yet. A false refusal costs nothing: the apply goes forward from a retired fence.
            raise CollectionStoreError("COLLECTION_STORE_RETIRED", (
                "the coordinator retired this store's fence, so the export can only go forward: run "
                "`exomem collections export --plan-id ID --why REASON` with the service stopped"))
        admission._require_fence(fence, marker)
        runtime = session.runtime()
        with manager.writer_authority_guard(vault_root=root):
            token = manager._fencing_token
            if not runtime.retire_idle_handle():
                raise CollectionStoreError(takeover.BUSY, "the live store is still borrowed")
            with session.writer(token) as writer:
                # Another host that adopted the replica since the export began holds rows this store lacks.
                admission._verify_acquisition(writer, runtime, fence, marker)
                with closing(connection.open_reader(session.path)) as reader:
                    _, work = plan(root, reader, plan_id=plan_id, views=False)
                    parents = _parents(reader)
                views, removed, missing_history = [], [], []
                for cid, planned in work.items():
                    transition = _transition(plan_id, cid)
                    manifest = collections.parse_manifest_bytes(
                        root, planned["manifest_path"], planned["manifest_text"].encode("utf-8"))
                    checkpointed = _checkpointed(planned["manifest_text"], manifest, transition).encode("utf-8")
                    for path, target in planned["targets"].items():
                        data = _read(root, path)
                        if target is None and data is None:
                            kind = writer.connection.execute("SELECT kind FROM projection_state WHERE path=?", (path,)).fetchone()
                            (missing_history if kind == ("history",) else removed).append((path,))
                        elif data is not None and (data == target or path == planned["manifest_path"]
                                                   and data == checkpointed):
                            views.append((_sha(data), path))
                    audit_history = records._audit_events(root, semantic_profile=manifest.semantic_profile)
                    if planned["checkpoint"] and audit_history.parsed and any(
                            event["transition_id"] == transition for event in audit_history.events):
                        # The log holds this checkpoint, so the legacy chain ends there. A checkpoint cut
                        # between its manifest and log flips is not in the chain, whatever the manifest
                        # names: the next export rewrites the manifest and checkpoints its parent again.
                        parents[cid] = transition
                with writer.handle.transaction() as conn:
                    session.require(token)
                    if authority.pending_create(conn) != intent:
                        raise CollectionStoreError("COLLECTION_STORE_CREATE_CONFLICT", "the export intent changed")
                    conn.executemany("UPDATE projection_state SET published_sha256=? WHERE path=?", views)
                    conn.executemany("DELETE FROM projection_state WHERE path=? AND kind='held'", removed)
                    conn.executemany("UPDATE projection_state SET state='pending' WHERE path=? AND kind='history'", missing_history)
                    conn.execute("INSERT OR REPLACE INTO store_meta(key,value) VALUES (?,?)", (
                        schema.META_EXPORT_PARENTS, json.dumps(parents, sort_keys=True, separators=(",", ":"))))
                    conn.execute("DELETE FROM store_meta WHERE key=?", (authority.PENDING_CREATE,))
                # The copy drops the intent from the replica, so other hosts adopt the store again; a
                # cut before it heals at the store's next publication, which sees the intent changed.
                published = admission._publish_current_epoch(session, writer, token)
            if published.status != "published":
                raise CollectionStoreError("COLLECTION_STORE_FLUSH_PENDING",
                                           f"replica publication: {published.reason or published.status}")
            manager._renew_collection_store(token)
    return _report("abandon", "abandoned", status="abandoned", plan_id=plan_id, store_id=marker["store_id"],
                   views_recorded=len(views), held_removed=len(removed), checkpoints=sorted(
                       cid for cid in work if parents.get(cid) == _transition(plan_id, cid)))


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
        if not session.path.exists():
            if completed_retirement_binding(root, fence_client) is None:
                raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "no completed export remains")
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
        completed = completed_retirement_binding(root, fence_client, store_id)
        if completed is None:
            raise CollectionStoreError("COLLECTION_STORE_MARKER_CONFLICT", "no finished export retired this host's "
                                       "store, so it stays; export the vault first")
        with manager.writer_authority_guard(vault_root=root):
            token = manager._fencing_token
            state_migration.retire_collection_store_compatibility(root, authority_check=lambda: session.require(token))
            session.require(token)
            _tombstone_store(session, completed[0])
    return _report("retire-local", "retired", status="retired", store_id=store_id, exported_at=completed[0])
