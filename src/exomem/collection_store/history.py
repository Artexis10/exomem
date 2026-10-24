"""Pure canonical history, shared by inspection, row history and generated pages."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path

from .. import mutation_terminal, records
from . import chain, tokens, typed_storage, types

# A6 fixes the generated history page limits.
MAIN_ENTRIES = 200
PAGE_ENTRIES = 500
# The history release cache admits sixteen exact reader signatures per handle.
MAX_READER_WINDOWS = 16


def _rows(cursor):
    names = tuple(column[0] for column in cursor.description)
    for row in cursor:
        yield dict(zip(names, row, strict=True))


def _fail(txn, reason):
    raise chain.StoreChainError(txn["commit_seq"], reason)


class PrefixProofs:
    """Bounded scalar proofs; the handle's accounted-change boundary owns invalidation."""

    def __init__(self):
        self.clear()

    def clear(self):
        self.store = (0, None)
        self.collections = OrderedDict()

    def remember(self, cid, proof):
        self.collections[cid] = proof
        self.collections.move_to_end(cid)
        while len(self.collections) > 128:
            self.collections.popitem(last=False)


def effects(conn, txn):
    """Reconstruct precisely the native v1 event's effect material."""
    receipt = json.loads(txn["receipt_json"])
    if txn["operation"] in mutation_terminal.CONTROL_OPERATIONS:
        return {name: receipt[name] for name in ("counts", "ids")}
    if txn["operation"] == "rebaseline":
        return {name: receipt[name] for name in
                ("acknowledged_gap_codes", "gap_fingerprint", "checkpoint_snapshot_hash")}
    found = list(_rows(conn.execute("SELECT * FROM audit_effects WHERE txn_id=? ORDER BY ordinal",
                                   (txn["txn_id"],))))
    for effect in found:
        identity = conn.execute("SELECT i.collection_id,i.item_key,v.payload_hash,v.txn_id FROM items i "
                                "JOIN version_identity v ON i.row_id=v.row_id WHERE i.row_id=? AND v.row_version=?",
                                (effect["row_id"], effect["version_after"])).fetchone()
        before = effect["version_before"]
        moved = txn["operation"] == "view_move"
        expected = before if moved else 1 if before is None else before + 1
        if (identity is None or identity[:3] != (txn["collection_id"], effect["item_key"], effect["hash_after"])
                or effect["version_after"] != expected or (not moved and identity[3] != txn["txn_id"])):
            _fail(txn, "effect version binding differs")
        prior = None if before is None else conn.execute(
            "SELECT payload_hash FROM version_identity WHERE row_id=? AND row_version=?",
            (effect["row_id"], before)).fetchone()
        if (prior[0] if prior is not None else None) != effect["hash_before"]:
            _fail(txn, "effect predecessor payload differs")
    if txn["operation"] == "view_move":
        return {"old_path": receipt["affected_paths"][0], "path": receipt["affected_paths"][1],
                "generation": txn["generation_before"], "version": found[0]["version_before"]}
    material = []
    for effect in found:
        if effect["row_id"] is None or effect["version_after"] is None:
            _fail(txn, "native effect has no immutable version")
        identity = conn.execute("SELECT payload_hash,schema_version FROM version_identity WHERE row_id=? AND row_version=?",
                                (effect["row_id"], effect["version_after"])).fetchone()
        values, body = typed_storage.version_values(conn, effect["row_id"], effect["version_after"])
        if (identity is None or identity[0] != effect["hash_after"]
                or tokens.payload_hash(identity[1], effect["item_key"], values, body) != identity[0]):
            _fail(txn, "immutable version differs from its audit effect")
        material.append({"key": effect["item_key"], "payload_hash": effect["hash_after"],
                         "sources": [row[0] for row in conn.execute(
                             "SELECT source_ref FROM item_sources WHERE row_id=? AND row_version=? ORDER BY ordinal",
                             (effect["row_id"], effect["version_after"]))]})
    return material if txn["operation"] == "bulk_upsert" else material[0] if material else None


def verify_event(conn, txn):
    """Verify one immutable envelope without reading any activity file."""
    try:
        if txn["operation"] == "legacy_import":
            from . import legacy_import

            envelope = json.loads(txn["receipt_json"])
            digest = legacy_import._event_hash(envelope)
            original = txn["legacy_event_json"]
            if original is not None:
                event = json.loads(original)
                profile = "planning" if txn["profile_operation"] is not None else "records"
                if (not records._valid_audit_event(event, profile)
                        or envelope["legacy_event_sha256"] != hashlib.sha256(original.encode()).hexdigest()
                        or event["transition_id"] != txn["transition_id"]):
                    _fail(txn, "imported original envelope differs")
            for name in ("collection_id", "transition_id", "generation_before", "generation_after",
                         "manifest_version_before", "manifest_version_after", "prev_event_hash", "actor", "why",
                         "profile_operation"):
                if envelope[name] != txn[name]:
                    _fail(txn, "imported envelope binding differs")
            if envelope["imported_at"] != txn["committed_at"]:
                _fail(txn, "imported time binding differs")
            if original is None:
                for row_id, key, version, payload, schema_version in conn.execute(
                    "SELECT v.row_id,i.item_key,v.row_version,v.payload_hash,v.schema_version FROM version_identity v "
                    "JOIN items i ON i.row_id=v.row_id WHERE v.txn_id=?", (txn["txn_id"],)):
                    values, body = typed_storage.version_values(conn, row_id, version)
                    if tokens.payload_hash(schema_version, key, values, body) != payload:
                        _fail(txn, "imported version payload differs")
        else:
            manifest = conn.execute(
                "SELECT manifest_hash FROM collection_manifests WHERE collection_id=? AND manifest_version=?",
                (txn["collection_id"], txn["manifest_version_after"])).fetchone()
            if manifest is None:
                _fail(txn, "event manifest is absent")
            event = {name: txn[name] for name in
                     ("transition_id", "collection_id", "operation", "generation_before", "generation_after",
                      "manifest_version_before", "manifest_version_after", "prev_event_hash", "committed_at",
                      "why", "request_hash")}
            event.update(manifest_hash=manifest[0], effects=effects(conn, txn))
            digest = hashlib.sha256(b"exomem-collection-event:v1\0" + records._canonical_json(event)).hexdigest()
        if digest != txn["event_hash"]:
            _fail(txn, "event hash differs from immutable material")
    except (KeyError, IndexError, TypeError, ValueError) as error:
        _fail(txn, f"invalid event material ({type(error).__name__})")


def verify(conn, cid, *, proofs=None, pending=None):
    """Verify a collection prefix and store chain, optionally including the writer's final insert.

    The persisted watermark is derived evidence. A cold handle verifies from genesis;
    an invalid watermark never turns intact canonical history into a reported gap.
    """
    proofs = proofs if proofs is not None else PrefixProofs()
    sequence, head = proofs.store
    for txn in _rows(conn.execute("SELECT * FROM txns WHERE commit_seq>? ORDER BY commit_seq", (sequence,))):
        if txn["commit_seq"] != sequence + 1:
            _fail(txn, "store sequence gap")
        sequence += 1
        try:
            head = tokens.store_head_hash(head, sequence, txn["event_hash"])
        except ValueError:
            _fail(txn, "malformed store hash")
        if head != txn["store_head_hash"]:
            _fail(txn, "store head differs")
    if chain.recorded_head(conn) != (sequence, head):
        raise chain.StoreChainError(sequence, "store_meta head disagrees with txns")
    prefix = proofs.collections.get(cid, (0, None, 0))
    through, business, generation = prefix
    watermark = conn.execute("SELECT verified_through_txn FROM collections WHERE collection_id=?", (cid,)).fetchone()[0]
    if watermark is not None and conn.execute(
            "SELECT 1 FROM txns WHERE txn_id=? AND collection_id=?", (watermark, cid)).fetchone() is None:
        through, business, generation = 0, None, 0
    txns = conn.execute("SELECT * FROM txns WHERE collection_id=? AND txn_id>? ORDER BY txn_id", (cid, through))
    for txn in _rows(txns):
        verify_event(conn, txn)
        if txn["prev_event_hash"] != business or txn["generation_before"] != generation:
            _fail(txn, "collection predecessor differs")
        if txn["generation_after"] != generation:
            if txn["generation_after"] != generation + 1:
                _fail(txn, "collection generation gap")
            business, generation = txn["event_hash"], txn["generation_after"]
        through = txn["txn_id"]
    if pending is not None:
        verify_event(conn, pending)
        if (pending["commit_seq"] != sequence + 1 or pending["prev_event_hash"] != business
                or pending["generation_before"] != generation
                or pending["store_head_hash"] != tokens.store_head_hash(head, sequence + 1, pending["event_hash"])):
            _fail(pending, "pending predecessor differs")
        through = pending["txn_id"]
        if pending["generation_after"] != generation:
            if pending["generation_after"] != generation + 1:
                _fail(pending, "pending generation gap")
            business, generation = pending["event_hash"], pending["generation_after"]
        sequence, head = pending["commit_seq"], pending["store_head_hash"]
    current = conn.execute("SELECT audit_head,generation FROM collections WHERE collection_id=?", (cid,)).fetchone()
    if tuple(current) != (business, generation):
        raise chain.StoreChainError(sequence, "collection head disagrees with transactions")
    proofs.store = sequence, head
    proofs.remember(cid, (through, business, generation))
    return through


def transactions(conn, cid, *, newest=True, after=0, ids=None, through=None):
    order = "DESC" if newest else "ASC"
    selected = " AND txn_id IN (SELECT value FROM json_each(?))" if ids is not None else ""
    bounded = " AND txn_id<=?" if through is not None else ""
    args = (cid, after, *((json.dumps(ids),) if ids is not None else ()), *((through,) if through is not None else ()))
    return _rows(conn.execute(
        f"SELECT * FROM txns WHERE collection_id=? AND txn_id>?{selected}{bounded} ORDER BY txn_id {order}", args))


def parent_transition(conn, txn):
    if txn["prev_event_hash"] is None:
        return "absent" if txn["operation"] == "create" else "baseline"
    parent = conn.execute("SELECT transition_id FROM txns WHERE collection_id=? AND event_hash=?",
                          (txn["collection_id"], txn["prev_event_hash"])).fetchone()
    if parent is None:
        _fail(txn, "public predecessor is absent")
    return parent[0]


def audit(writer, row, *, complete=True):
    """Preserve imported provenance independently of present-day visibility."""
    from .. import record_governance

    verify(writer.connection, row["collection_id"], proofs=writer.handle.release_cache.history)
    incomplete = {"status": "history_incomplete", "gaps": []}
    if not complete:
        return incomplete
    if row["legacy_audit_status"] is None:
        return {"status": "ok" if row["audit_head"] else "baseline", "gaps": []}
    checkpoint = writer.connection.execute("SELECT receipt_json FROM txns WHERE txn_id=?", (row["created_txn"],)).fetchone()
    captured = json.loads(checkpoint[0])
    paths = captured.get("bounded_inspection_paths")
    if not isinstance(paths, list) or any(not isinstance(path, str) or not writer._operation.allows_history_path(path)
                                          for path in paths):
        return incomplete
    result = record_governance._inspection_audit(captured.get("bounded_inspection"))
    acknowledged = writer.connection.execute(
        "SELECT * FROM txns WHERE collection_id=? AND operation='rebaseline' ORDER BY txn_id DESC LIMIT 1",
        (row["collection_id"],))
    latest = next(_rows(acknowledged), None)
    if latest is not None:
        receipt = json.loads(latest["receipt_json"])
        result = {"status": "acknowledged_gap", "gaps": [], "discontinuity": {
            "provenance_continuity": False, "prior_head": parent_transition(writer.connection, latest),
            "acknowledged_gap_codes": receipt["acknowledged_gap_codes"], "rationale": latest["why"],
            "checkpoint_transition": latest["transition_id"], "gap_fingerprint": receipt["gap_fingerprint"],
            "checkpoint_snapshot_hash": receipt["checkpoint_snapshot_hash"]}}
        result["discontinuities"] = [result["discontinuity"]]
    elif result["status"] != row["legacy_audit_status"]:
        return incomplete
    return result


def event_paths(txn):
    receipt = json.loads(txn["receipt_json"])
    if txn["legacy_event_json"] is not None:
        original = json.loads(txn["legacy_event_json"])
        return tuple(original[name] for name in ("manifest_path", "source_path", "canonical_path"))
    return tuple(receipt.get("affected_paths", ()))


def agent_event(conn, manifest, txn):
    """Use the established content-free agent event envelope."""
    if txn["legacy_event_json"] is not None:
        return records._project_agent_audit_event(json.loads(txn["legacy_event_json"]))
    receipt = json.loads(txn["receipt_json"])
    found = conn.execute("SELECT item_key,version_before,version_after,hash_before,hash_after FROM audit_effects "
                         "WHERE txn_id=? ORDER BY ordinal LIMIT 1", (txn["txn_id"],)).fetchone()
    before_manifest = conn.execute("SELECT manifest_hash FROM collection_manifests WHERE collection_id=? AND manifest_version=?",
                                   (manifest.collection_id, txn["manifest_version_before"])).fetchone()
    after_manifest = conn.execute("SELECT manifest_hash FROM collection_manifests WHERE collection_id=? AND manifest_version=?",
                                  (manifest.collection_id, txn["manifest_version_after"])).fetchone()
    key = found[0] if found is not None else None
    paths = receipt.get("affected_paths", ())
    operation = txn["profile_operation"] or txn["operation"]
    if operation == "view_edit":
        # The existing Records audit wire names accepted item edits as updates.
        operation = "update"
    event = {"transition_id": txn["transition_id"], "parent_id": parent_transition(conn, txn),
             "operation": operation, "item_key": key,
             "canonical_path": paths[0] if paths else manifest.path,
             "before_manifest_hash": before_manifest[0] if before_manifest else None,
             "after_manifest_hash": after_manifest[0] if after_manifest else None,
             "before_item_hash": receipt.get("before_item_hash"), "after_item_hash": receipt.get("after_item_hash"),
             "before_container_hash": receipt.get("before_container_hash"),
             "after_container_hash": receipt.get("after_container_hash"), "rationale": txn["why"]}
    if txn["operation"] == "rebaseline":
        event.update({name: receipt[name] for name in ("continuity", "acknowledged_gap_codes", "gap_fingerprint",
                                                      "checkpoint_snapshot_hash", "minimum_reader_version")})
    return event


def agent_history(writer, manifest):
    from .. import structured_collections as collections
    from ..governance import egress
    from . import governance

    row = writer._collection_row(manifest)
    manifest = writer._collection_manifest(row)[0]
    assessment = audit(writer, row)
    events, complete = [], assessment["status"] != "history_incomplete"
    control_release = {}
    field_plan = writer._operation.field_plan(manifest)
    if not field_plan.owner and field_plan.whole_fields != set(manifest.schema.fields):
        return {"status": "history_incomplete", "complete": False, "truncated": False, "events": []}

    def released(row_id):
        item = writer.connection.execute("SELECT item_key FROM items WHERE row_id=? AND collection_id=?",
                                        (row_id, manifest.collection_id)).fetchone()
        if item is None:
            return False
        subject = next(iter(governance.iter_subjects(writer.connection, manifest.collection_id,
            writer._operation.logical_vault_id, row_key=item[0])), None)
        return subject is not None and writer._operation.decision(subject).level >= 6

    for txn in transactions(writer.connection, manifest.collection_id):
        if txn["operation"] in mutation_terminal.CONTROL_OPERATIONS:
            # Store reconciliation can name other collections; other controls name this whole collection.
            store_wide = txn["operation"] == "store_reconcile"
            if store_wide not in control_release:
                scope = (tuple(cid for cid, in writer.connection.execute("SELECT collection_id FROM collections"))
                         if store_wide else (manifest.collection_id,))
                try:
                    if not egress.unrestricted_content_access(writer.root, writer._operation.who):
                        writer._operation.refuse()
                    for cid in scope:
                        writer._operation.require_collection(cid)
                        writer._operation.require_complete_fields(cid)
                    control_release[store_wide] = True
                except collections.CollectionError:
                    control_release[store_wide] = False
            if not control_release[store_wide]:
                complete = False
                continue
        if any(not writer._operation.allows_history_path(path) for path in event_paths(txn)):
            complete = False
            continue
        targets = writer.connection.execute("SELECT row_id FROM audit_effects WHERE txn_id=?", (txn["txn_id"],))
        if any(row_id is not None and not released(row_id) for row_id, in targets):
            complete = False
            continue
        if txn["legacy_event_json"] is not None:
            legacy_event = json.loads(txn["legacy_event_json"])
            key = legacy_event.get("item_key")
            if key is not None:
                current = writer.connection.execute("SELECT row_id FROM items WHERE collection_id=? AND item_key=?",
                                                     (manifest.collection_id, key)).fetchone()
                if current is None:
                    # Only unrestricted readers can release an imported event with no current row metadata.
                    identity = f"exomem://{types.type_for_manifest(manifest).item_type}/{manifest.collection_id}/{key}"
                    permitted = (egress.unrestricted_content_access(writer.root, writer._operation.who)
                                 and governance.content_available(
                                     legacy_event["canonical_path"], identity,
                                     writer._operation.access, writer._operation.access_blocked,
                                     writer._operation.tombstones))
                else:
                    permitted = released(current[0])
                if not permitted:
                    complete = False
                    continue
        if txn["operation"] == "legacy_import" and txn["legacy_event_json"] is None:
            continue
        events.append(agent_event(writer.connection, manifest, txn))
        if len(events) > records._MAX_AGENT_AUDIT_HISTORY:
            break
    truncated = len(events) > records._MAX_AGENT_AUDIT_HISTORY
    # The existing audit envelope fixes these provenance status tokens.
    verified = complete and assessment["status"] not in {"gap", "history_incomplete"}
    return {"status": assessment["status"] if complete else "history_incomplete", "complete": verified,
            "truncated": truncated, "events": events[:records._MAX_AGENT_AUDIT_HISTORY],
            **{name: assessment[name] for name in ("discontinuity", "discontinuities") if name in assessment}}


def row_history(writer, manifest, *, item_key, limit=None, continuation=None):
    """Merge retained legacy corrections with canonical versions in the existing envelope."""
    from .. import record_governance, record_history, registry_history
    from .. import structured_collections as collections
    from ..governance import egress

    page_size, after = record_history._page_bounds(limit, continuation)
    cid, conn = manifest.collection_id, writer.connection
    manifest = writer._collection_manifest(writer._collection_row(manifest))[0]
    from . import governance

    subject = next(iter(governance.iter_subjects(conn, cid, writer._operation.logical_vault_id, row_key=item_key)), None)
    if subject is None or writer._operation.decision(subject).level < 6:
        raise collections.CollectionError("RECORD_NOT_FOUND", "record key does not exist")
    plan = writer._operation.field_plan(manifest)
    if not plan.owner and plan.whole_fields != set(manifest.schema.fields):
        raise collections.CollectionError("RECORD_NOT_FOUND", "record key does not exist")
    item = writer._item(cid, item_key)
    assessment = audit(writer, writer._collection_row(manifest))
    relative = record_history.history_dir(cid, item_key)
    names = registry_history.kept_names(writer.root, relative, suffix=record_history._SUFFIX) or []
    unchanged = registry_history.kept_names(writer.root, relative, suffix=record_history._UNCHANGED) or []
    legacy = {}
    missing, withheld = 0, False
    candidates = []
    cursor = conn.execute(
        "SELECT t.* FROM audit_effects e JOIN txns t ON t.txn_id=e.txn_id WHERE e.row_id=? "
        "UNION ALL SELECT * FROM txns WHERE collection_id=? AND legacy_event_json IS NOT NULL "
        "AND json_extract(legacy_event_json,'$.item_key')=? ORDER BY txn_id DESC",
        (item["row_id"], cid, item_key))
    for txn in _rows(cursor):
        if txn["legacy_event_json"] is not None:
            event = json.loads(txn["legacy_event_json"])
            if event.get("item_key") != item_key or event["operation"] in record_history._CREATIONS:
                continue
            if any(not writer._operation.allows_history_path(path) for path in event_paths(txn)):
                withheld = True
                continue
            legacy[event["transition_id"]] = event
            continue
        effect = conn.execute("SELECT version_before,version_after,hash_before,hash_after FROM audit_effects "
                              "WHERE txn_id=? AND row_id=?", (txn["txn_id"], item["row_id"])).fetchone()
        if effect is None or effect[0] is None or effect[2] == effect[3]:
            continue
        if any(not writer._operation.allows_history_path(path) for path in event_paths(txn)):
            withheld = True
            continue
        moment = dt.datetime.fromisoformat(txn["committed_at"].replace("Z", "+00:00"))
        # The position includes the immutable sequence, so same-second corrections paginate distinctly.
        version = registry_history.version_id(moment + dt.timedelta(microseconds=txn["commit_seq"]), txn["transition_id"])
        candidates.append({"version": version, "txn": txn, "effect": effect})
    accounted = set()
    for name in unchanged:
        tag = name.rsplit("-", 1)[-1].split(".", 1)[0]
        matches = [transition for transition in legacy if transition.startswith(tag)]
        if len(matches) == 1:
            accounted.add(matches[0])
        elif matches:
            withheld = True  # An old empty marker cannot disambiguate colliding transition prefixes.
            accounted.update(matches)
    for name in names:
        tag = name.rsplit("-", 1)[-1].split(".", 1)[0]
        matches = [transition for transition in legacy if transition.startswith(tag)]
        if not matches:
            continue
        version = name[:-len(record_history._SUFFIX)]
        text = None
        transition = matches[0] if len(matches) == 1 else None
        if transition is None:
            # Only a prefix collision needs retained payloads before the selected page.
            text = registry_history.read_kept(writer.root, relative, name)
            try:
                retained = json.loads(text) if text is not None else None
            except ValueError:
                retained = None
            transition = retained.get("transition_id") if isinstance(retained, dict) else None
        if transition not in legacy:
            withheld = True
            continue
        accounted.add(transition)
        candidates.append({"version": version, "name": name, "text": text})
    missing = len(set(legacy) - accounted)
    # Import precedes every native correction, even when legacy file clocks are newer.
    # The public version is a position, so locate that position in canonical order.
    position = next((index + 1 for index, revision in enumerate(candidates) if revision["version"] == after), len(candidates))
    older = candidates if after is None else candidates[position:]
    page = []
    for candidate in older[:page_size]:
        version = candidate["version"]
        if "name" in candidate:
            text = candidate["text"]
            if text is None:
                text = registry_history.read_kept(writer.root, relative, candidate["name"])
            try:
                retained = json.loads(text) if text is not None else None
            except ValueError:
                retained = None
            if isinstance(retained, dict) and not writer._operation.allows_history_path(retained.get("path", "")):
                withheld = True
                revision = {"version": version, "status": "unreadable"}
            else:
                revision = record_history._revision(text, version)
        else:
            txn, effect = candidate["txn"], candidate["effect"]
            values, body = typed_storage.version_values(conn, item["row_id"], effect[0])
            receipt = json.loads(txn["receipt_json"])
            revision = {"version": version, "status": "available", "at": txn["committed_at"],
                "operation": txn["profile_operation"] or txn["operation"], "why": txn["why"],
                "transition_id": txn["transition_id"], "before_item_hash": receipt.get("before_item_hash"),
                "after_item_hash": receipt.get("after_item_hash"),
                "prior": {"values": values, "body": body, "payload_hash": effect[2]},
                "binding": None, "actor": {"audience": txn["actor"]}}
        page.append(record_history.project_revision(writer.root, writer._operation.policy, writer._operation.who, revision))
    truncated = len(older) > len(page)
    result = {"collection_id": cid, "item_key": item_key,
              "status": "unverified" if withheld or assessment["status"] in {"gap", "history_incomplete"}
                        else "unavailable_legacy" if missing else "complete",
              **({"unavailable": missing} if missing else {}), "retained": len(candidates), "returned": len(page),
              "truncated": truncated, "continuation": page[-1]["version"] if truncated else None, "revisions": page}
    return egress.project(record_governance._RecordEnvelope(result), egress.LEVEL_FULL, kind="record_history") or {}


def main_path(manifest):
    return (Path(manifest.path).parent / "_history.md").as_posix()


def page_path(manifest, number):
    return (Path(manifest.path).parent / "_history" / f"{number:04d}.md").as_posix()


def page_subject(path):
    """Generated history has this canonical metadata, independent of editable file bytes."""
    from ..governance.membership import MetadataSubject

    return MetadataSubject(path, (path,), (), (), (), ())


@dataclass(frozen=True, slots=True)
class _Window:
    through_txn: int = 0
    released_count: int = 0
    newest: tuple[int, ...] = ()
    unresolved: bool = False
    digests: tuple[tuple[str, str, str], ...] = ()
    queued: tuple[int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class _FileInputs:
    """The file-held inputs of a history release; ``key`` names the access state and tombstones for a page's epoch."""

    policy: object
    tombstones: frozenset
    access: dict
    access_blocked: bool
    key: tuple


def _file_inputs(root):
    """Read a release's file-held inputs once; a page's epoch and the repair key both come from here, so they cannot drift."""
    from .. import access
    from ..governance import lifecycle, policy

    loaded, tombstones = policy.load(root), lifecycle.tombstoned_paths(root)
    access_fingerprint, rules, blocked = access._policy_state(root)
    return _FileInputs(loaded, tombstones, rules, blocked, (access_fingerprint, blocked, tuple(sorted(tombstones))))


def release_inputs(root):
    """One digest over every file-held input of a history release; it moves when any of them does.

    The store thread keys its history repair on this, so a changed policy, access file, blocked flag
    or tombstone marks the pages pending without a census render. It reads no history.
    """
    found = _file_inputs(root)
    return hashlib.sha256(json.dumps([found.policy.fingerprint, *found.key]).encode()).hexdigest()


class PageContext:
    """One release projector; retained windows contain only bounded scalar evidence."""

    def __init__(self, conn, root, manifest, *, cache=None, through=None):
        from ..governance import membership
        from . import governance

        self.conn, self.root, self.manifest, self.cache = conn, root, manifest, cache
        self.through = through
        found = _file_inputs(root)
        self.policy, self.tombstones, self.access, self.access_blocked = (
            found.policy, found.tombstones, found.access, found.access_blocked)
        # A failed policy read cannot turn its empty scopes into permission to publish history.
        if self.policy.blocked:
            governance.OperationAuthorization.refuse()
        self.membership = membership
        (self.path, self.source, self.layout, self.view_mode, self.metadata, self.declared,
         self.make, raw, version, digest) = governance.subject_factory(conn, manifest.collection_id, "unbound")
        self.manifest_subject = self.make(self.path, None, self.path, "collection", raw, version, digest, "manifest")
        basis = self.manifest_subject.basis
        self.epoch = (self.path, self.source, self.layout, self.view_mode, version, digest,
                      basis.type_name, basis.type_version, basis.declaration_hash, basis.default_audience, found.key)
        self.subjects, self.windows, self.reader_sets = OrderedDict(), OrderedDict(), OrderedDict()
        self.unresolved = False
        if cache is not None:
            cache.check()
        verify(conn, manifest.collection_id, proofs=None if cache is None else cache.history)

    def readers(self, path):
        from ..governance import projections
        from . import governance

        if path not in self.reader_sets:
            if not governance.content_available(path, None, self.access, self.access_blocked, self.tombstones):
                return ()
            page = page_subject(path)
            scopes = tuple(sorted(set(self.membership.evaluate_metadata(page, self.policy))
                                  | set(self.membership.evaluate_metadata(self.manifest_subject.basis.subject, self.policy))))
            self.reader_sets[path] = tuple(projections.page_reader_states(self.policy, scopes))
        self.reader_sets.move_to_end(path)
        readers = self.reader_sets[path]
        while len(self.reader_sets) > 128:
            self.reader_sets.popitem(last=False)
        return readers

    def _subject(self, row):
        from . import governance

        row_id, key, version, digest, view_path, raw = row
        if row_id in self.subjects:
            self.subjects.move_to_end(row_id)
            return self.subjects[row_id]
        meta = governance._metadata(raw)
        if meta["projects"] != self.metadata["projects"]:
            raise ValueError("canonical row project differs from its manifest")
        path = self.path if self.view_mode == "summary" else self.source if self.layout == "markdown-log" else view_path
        subject = self.make(f"exomem://{self.declared.item_type}/{self.manifest.collection_id}/{key}",
                            row_id, path, self.declared.item_type, raw, version, digest, "row")
        self.subjects[row_id] = subject
        while len(self.subjects) > 128:
            self.subjects.popitem(last=False)
        return subject

    def _allows(self, subject, readers, historical=None):
        from ..governance.decisions import decide
        from . import governance

        if subject.basis.quarantined:
            return False
        metadata = subject.basis.subject
        if historical is not None:
            metadata = replace(metadata, path=historical)
        if not governance.content_available(metadata.path, subject.basis.identity,
                                            self.access, self.access_blocked, self.tombstones):
            return False
        scopes = self.membership.evaluate_metadata(metadata, self.policy)
        scopes, effective = governance.subject_policy(subject.basis, scopes, self.policy)
        return all(decide(scopes, audience=audience, purpose=purpose, policy=effective,
                          active_grants=self.policy.grants).level >= 6 for audience, purpose in readers)

    def _project(self, txn, effects, readers):
        # These opaque-scope protocol controls cannot be released from a current-data completeness check.
        if not readers or txn["operation"] in mutation_terminal.CONTROL_OPERATIONS:
            return None
        historical_paths = event_paths(txn)
        paths, omitted = [], False
        for row in effects:
            if row[0] is None:
                omitted = True
                continue
            subject = self._subject(row)
            if (not self._allows(subject, readers)
                    or any(not self._allows(subject, readers, path) for path in historical_paths)):
                omitted = True
                continue
            paths.append(subject.basis.subject.path)
        if effects and not paths:
            return None
        original = None
        if txn["legacy_event_json"] is not None:
            original = json.loads(txn["legacy_event_json"])
            key = original.get("item_key")
            if key is not None:
                row = self.conn.execute("SELECT row_id,item_key,row_version,payload_hash,view_path,governance_json "
                    "FROM items WHERE collection_id=? AND item_key=?", (self.manifest.collection_id, key)).fetchone()
                if row is None:
                    self.unresolved = True
                    return None
                subject = self._subject(row)
                if (not self._allows(subject, readers)
                        or any(not self._allows(subject, readers, path) for path in historical_paths)):
                    return None
                paths = [original["canonical_path"]]
            elif (not self._allows(self.manifest_subject, readers)
                  or any(not self._allows(self.manifest_subject, readers, path) for path in historical_paths)):
                return None
        elif not effects and (not self._allows(self.manifest_subject, readers)
                              or any(not self._allows(self.manifest_subject, readers, path) for path in historical_paths)):
            return None
        return {"at": ("Imported " if txn["operation"] == "legacy_import" else "") + txn["committed_at"],
                "operation": original["operation"] if original else txn["profile_operation"] or txn["operation"],
                "actor": "agent", "why": None if omitted else original["rationale"] if original else txn["why"],
                "paths": paths}

    def events(self, readers, *, after=0, ids=None, newest=False):
        iterator = iter(transactions(self.conn, self.manifest.collection_id, newest=newest, after=after,
                                     ids=ids, through=self.through))
        while batch := list(islice(iterator, 64)):
            effects = {}
            for row in self.conn.execute(
                "SELECT e.txn_id,i.row_id,i.item_key,i.row_version,i.payload_hash,i.view_path,i.governance_json "
                "FROM audit_effects e LEFT JOIN items i ON i.row_id=e.row_id "
                "WHERE e.txn_id IN (SELECT value FROM json_each(?)) ORDER BY e.txn_id,e.ordinal",
                (json.dumps([txn["txn_id"] for txn in batch]),),
            ):
                effects.setdefault(row[0], []).append(row[1:])
            for txn in batch:
                yield txn["txn_id"], self._project(txn, effects.get(txn["txn_id"], ()), readers)

    def window(self, path):
        return self._window(self.readers(path))

    def _window(self, readers):
        key = (self.manifest.collection_id, self.epoch, self.policy.fingerprint, readers)
        if readers in self.windows:
            self.windows.move_to_end(readers)
            return self.windows[readers]
        previous = self.cache.history_windows.get(key) if self.cache is not None else None
        future = previous is not None and self.through is not None and previous.through_txn > self.through
        if future:
            previous = None
        previous = previous or _Window()
        through, count, newest = previous.through_txn, previous.released_count, deque(previous.newest, maxlen=MAIN_ENTRIES + PAGE_ENTRIES)
        self.unresolved = previous.unresolved
        for txn_id, event in self.events(readers, after=through):
            through = txn_id
            if event is not None:
                count += 1
                newest.appendleft(txn_id)
        window = (previous if through == previous.through_txn else
                  _Window(through, count, tuple(newest), self.unresolved,
                          previous.digests if tuple(newest) == previous.newest and count == previous.released_count else (),
                          previous.queued))
        self.windows[readers] = window
        while len(self.windows) > MAX_READER_WINDOWS:
            self.windows.popitem(last=False)
        if self.cache is not None and not future:
            self.cache.remember_history(key, window)
        return window

    def exists(self, path):
        if path == main_path(self.manifest):
            return True
        return self.window(path).released_count > MAIN_ENTRIES + (int(Path(path).stem) - 1) * PAGE_ENTRIES

    def _navigation(self, path, window):
        targets = []
        if path == main_path(self.manifest):
            if window.released_count > MAIN_ENTRIES:
                number = (window.released_count - MAIN_ENTRIES - 1) // PAGE_ENTRIES + 1
                targets.append((page_path(self.manifest, number), "Older history"))
        else:
            targets.append((main_path(self.manifest), "Latest history"))
            if (number := int(Path(path).stem)) > 1:
                targets.append((page_path(self.manifest, number - 1), "Older history"))
        # Different reader domains do not prove the target's partition; omit unproved navigation.
        return tuple((target, label) for target, label in targets
                     if self.readers(target) == self.readers(path) and self.exists(target))

    def _selections(self, paths):
        result, deep = {}, {}
        for path in paths:
            window = self.window(path)
            if path == main_path(self.manifest):
                result[path] = window.newest[:MAIN_ENTRIES]
                continue
            start = (int(Path(path).stem) - 1) * PAGE_ENTRIES
            length = min(PAGE_ENTRIES, max(0, window.released_count - MAIN_ENTRIES - start))
            first, last = window.released_count - start - length, window.released_count - start
            if last <= len(window.newest):
                result[path] = window.newest[first:last] if length else ()
            elif length:
                deep.setdefault(self.readers(path), []).append((path, start, start + length))
            else:
                result[path] = ()
        for readers, ranges in deep.items():
            values = {path: [] for path, _, _ in ranges}
            index = 0
            stop = max(end for _, _, end in ranges)
            for txn_id, event in self.events(readers):
                if event is None:
                    continue
                for path, start, end in ranges:
                    if start <= index < end:
                        values[path].append(txn_id)
                index += 1
                if index >= stop:
                    break
            result.update({path: tuple(reversed(ids)) for path, ids in values.items()})
        return result

    def _render_selected(self, path, ids):
        readers, window = self.readers(path), self.window(path)
        events = [event for _, event in self.events(readers, ids=ids, newest=True) if event is not None]
        navigation = self._navigation(path, window)
        text = _render(events, navigation)
        digest = hashlib.sha256(text.encode()).hexdigest()
        self._remember_digest(path, ids, navigation, digest)
        return int.from_bytes(bytes.fromhex(digest)[:6], "big"), text

    def render_many(self, paths):
        return {path: self._render_selected(path, ids) for path, ids in self._selections(paths).items()}

    def _remember_digest(self, path, ids, navigation, digest):
        window = self.window(path)
        latest = (window.released_count - MAIN_ENTRIES + PAGE_ENTRIES - 1) // PAGE_ENTRIES
        if path not in (main_path(self.manifest), page_path(self.manifest, latest)):
            return
        selection = hashlib.sha256(json.dumps((ids, navigation)).encode()).hexdigest()
        digests = (*tuple(item for item in window.digests if item[0] != path), (path, selection, digest))[-2:]
        window = replace(window, digests=digests)
        readers = self.readers(path)
        self.windows[readers] = window
        while len(self.windows) > MAX_READER_WINDOWS:
            self.windows.popitem(last=False)
        if self.cache is not None:
            self.cache.remember_history((self.manifest.collection_id, self.epoch, self.policy.fingerprint, readers), window)

    def digests(self, paths):
        iterator = iter(paths)
        # Inspection can assess an explicit repair backlog without retaining all old page selections or text.
        while batch := tuple(islice(iterator, 64)):
            for path, ids in self._selections(batch).items():
                window = self.window(path)
                navigation = self._navigation(path, window)
                selection = hashlib.sha256(json.dumps((ids, navigation)).encode()).hexdigest()
                digest = next((digest for actual, key, digest in window.digests
                               if actual == path and key == selection), None)
                if digest is None:
                    _, text = self._render_selected(path, ids)
                    digest = hashlib.sha256(text.encode()).hexdigest()
                yield path, digest

    def digest(self, path):
        return next(self.digests((path,)))[1]


def render_page(conn, root, manifest, path, *, cache=None):
    return PageContext(conn, root, manifest, cache=cache).render_many((path,))[path]


def _render(content, navigation):
    lines = ["# Collection history", "", "Generated history. Changes to this page are replaced.", ""]
    for path, label in navigation:
        lines.extend(("[[" + path + "|" + label + "]]", ""))
    for event in content:
        lines.extend((f"## {event['at']} {event['operation']}", "", f"Actor: {event['actor']}", ""))
        if event["why"] is not None:
            lines.extend(("Reason: " + event["why"].replace("\n", " "), ""))
        for target in event["paths"]:
            lines.append("- [[" + target + "]]")
        if event["paths"]:
            lines.append("")
    return "\n".join(lines)


@dataclass(frozen=True, slots=True)
class QueueSweep:
    key: tuple[str, str]
    through: int
    raw_count: int
    intervals: tuple[tuple[int, int], ...]
    interval_index: int = 0
    next_number: int = 1
    counts: tuple[tuple[tuple, int], ...] = ()
    prepared: tuple[int, bool] | None = None
    excess_domains: bool = False
    unresolved: bool = False

    @property
    def done(self):
        return self.interval_index == len(self.intervals)


def _sweep_key(context):
    digest = hashlib.sha256(json.dumps((context.epoch, context.policy.fingerprint)).encode()).hexdigest()
    return context.manifest.collection_id, digest


def _last_page(count):
    return max(0, (count - MAIN_ENTRIES + PAGE_ENTRIES - 1) // PAGE_ENTRIES)


def _intervals(ranges):
    merged = []
    for start, end in sorted(ranges):
        if start > end:
            continue
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = merged[-1][0], max(end, merged[-1][1])
        else:
            merged.append((start, end))
    return tuple(merged)


def _start_sweep(context):
    cid, cache = context.manifest.collection_id, context.cache
    through = context.conn.execute("SELECT COALESCE(MAX(txn_id),0) FROM txns WHERE collection_id=?", (cid,)).fetchone()[0]
    head = context.window(main_path(context.manifest))
    same = [(key, window) for key, window in cache.history_windows.items()
            if key[:3] == (cid, context.epoch, context.policy.fingerprint)]
    queued = head.queued
    cold = queued is None or not same or any(window.queued is None for _, window in same)
    raw = context.conn.execute("SELECT COUNT(*) FROM txns WHERE collection_id=? AND txn_id>? AND txn_id<=?",
                               (cid, 0 if cold else queued[0], through)).fetchone()[0]
    raw += 0 if cold else queued[2]
    counts, ranges = [(context.readers(main_path(context.manifest)), head.released_count)], []
    if cold:
        ranges.append((1, _last_page(raw)))
    else:
        ranges.append((_last_page(queued[2]) + 1, _last_page(raw)))
        for key, previous in same:
            current = context._window(key[3])
            count = current.released_count
            if key[3] != counts[0][0]:
                counts.append((key[3], count))
            if count != previous.queued[1]:
                older = max(0, previous.queued[1] - MAIN_ENTRIES)
                ranges.append((older // PAGE_ENTRIES + 1, _last_page(count)))
    intervals = _intervals(ranges)
    return QueueSweep(_sweep_key(context), through, raw, intervals,
                      next_number=intervals[0][0] if intervals else 1, counts=tuple(counts),
                      unresolved=any(window.unresolved for window in context.windows.values()))


def _advance_sweep(sweep):
    index, number = sweep.interval_index, sweep.next_number + 1
    if number > sweep.intervals[index][1]:
        index += 1
        number = sweep.intervals[index][0] if index < len(sweep.intervals) else number
    return replace(sweep, interval_index=index, next_number=number, prepared=None)


def queue_pages(writer, manifest, *, stopped, limit):
    """Queue a fixed discovery target, preserving positive intents and negative holes across cutoffs."""
    cache = writer.handle.release_cache
    context = PageContext(writer.connection, writer.root, manifest, cache=cache)
    key = _sweep_key(context)
    sweep = cache.queued_history_sweep()
    if sweep is not None and sweep.key != key:
        if sweep.key[0] != manifest.collection_id:
            return False
        sweep = None
    if sweep is None:
        sweep = _start_sweep(context)
    bounded = PageContext(writer.connection, writer.root, manifest, cache=cache, through=sweep.through)
    prepared = []
    while not sweep.done and len(prepared) < min(limit, 64):
        if stopped():
            break
        number = sweep.next_number
        if sweep.prepared is None:
            path = page_path(manifest, number)
            readers = bounded.readers(path)
            count = next((value for domain, value in sweep.counts if domain == readers), None)
            if count is None:
                window = bounded._window(readers)
                count = window.released_count
                sweep = replace(sweep, unresolved=sweep.unresolved or window.unresolved)
                if len(sweep.counts) < MAX_READER_WINDOWS:
                    sweep = replace(sweep, counts=(*sweep.counts, (readers, count)))
                else:
                    sweep = replace(sweep, excess_domains=True)
            sweep = replace(sweep, prepared=(number, count > MAIN_ENTRIES + (number - 1) * PAGE_ENTRIES))
            if stopped():
                break
        prepared.append((page_path(manifest, number), sweep.prepared[1]))
        sweep = _advance_sweep(sweep)
    if prepared:
        known = dict(writer.connection.execute(
            "SELECT path,state FROM projection_state WHERE path IN (SELECT value FROM json_each(?))",
            (json.dumps([path for path, _ in prepared]),)))
        for path, exists in prepared:
            if (exists or path in known) and known.get(path) != "pending":
                writer._pending_basis(path, manifest.collection_id, "history", 0)
    if sweep.done:
        if not sweep.excess_domains:
            for readers, count in sweep.counts:
                cache.stage_history_queue((manifest.collection_id, context.epoch, context.policy.fingerprint, readers),
                                          (sweep.through, count, sweep.raw_count))
        cache.stage_history_sweep(None)
    else:
        cache.stage_history_sweep(sweep)
    return sweep.done
