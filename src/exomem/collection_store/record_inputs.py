"""Passive single-record candidates shared by preparation and publication."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field

from .. import origin_bindings, planning, provenance, record_formats, records
from .. import structured_collections as collections
from . import connection, edit_back, views


@dataclass
class PreparedRecord:
    name: str
    collection: str
    arguments: dict
    manifest: object
    store_identity: dict
    container_hash: str
    key: str
    values: dict
    body: str
    before: dict | None
    resumed: dict | None
    held_basis: dict | None
    hold_candidate: dict
    changes: dict | None = None
    delete_fields: tuple = ()
    prior_values: dict | None = None
    supplied_body: str | None = None
    error: collections.CollectionError | None = None
    origin: object = None
    adoption: object = None
    guards: list = field(default_factory=list)
    request_hash: str | None = None
    recheck_in_leaf: bool = False


@dataclass(frozen=True)
class DetachedResume:
    path: str | None = None
    raw: bytes | None = None
    guard: object = None
    native: object = None
    error: Exception | None = None


def from_snapshot(writer, name, collection, arguments):
    """Read a candidate in the controller's passive snapshot; return no live resource."""
    action = "append" if name == "append_record" else arguments["operation"]
    # These private controls do not belong to the canonical request identity.
    public = {key: value for key, value in arguments.items()
              if key not in ("request_id", "_prepare", "_validate_inputs")}
    _identity, _digest, replay, row, manifest, declared = writer._identified_collection(
        action, collection, public, arguments["request_id"])
    if replay is not None:
        return None, replay
    writer._rows_writable(manifest, action)
    prepare = append if name == "append_record" else update
    candidate = prepare(writer, row, manifest, declared, {**arguments, **deepcopy(public)})
    candidate.collection = collection
    candidate.request_hash = _digest
    return candidate, None


def observe(writer, candidate):
    if candidate.adoption is not None:
        candidate.adoption = writer._observe_detached_resume(candidate.manifest, candidate.resumed,
                                                            admitted=candidate.adoption)


def acquire(root, candidate, *, lifetime):
    if candidate.error is not None:
        return
    try:
        candidate.origin = origin_bindings.acquire_origin_inputs(root, candidate.body,
            before_source=candidate.before["body"] if candidate.name == "update_record" else None,
            lifetime=lifetime)
    except provenance.OriginError as error:
        refused = collections.CollectionError(error.code, error.reason)
        if candidate.name != "append_record":
            raise refused from error
        candidate.error = refused


def requires_acquisition(candidate):
    """Use the authoring protocol parser before retaining direct mutation authority."""
    try:
        return origin_bindings._authored_origin(candidate.body,
            candidate.before["body"] if candidate.name == "update_record" else None) is not None
    except provenance.OriginError:
        return True  # Acquisition preserves the leaf's existing origin-error disposition.


def legacy_origin(root, candidate):
    """Keep existing staging-preview callers on their original synchronous origin contract."""
    try:
        return origin_bindings.prepare_origin_inputs(root, candidate.body,
            before_source=candidate.before["body"] if candidate.name == "update_record" else None)
    except provenance.OriginError as error:
        raise collections.CollectionError(error.code, error.reason) from error


def recheck_snapshot(writer, candidate):
    """Reauthorize and compare canonical data in the controller's fresh final snapshot."""
    row, manifest, declared = writer._collection(candidate.collection)
    recheck_selected(writer, candidate, row, manifest, declared)


def recheck_selected(writer, candidate, row, manifest, declared):
    """Recheck against the leaf's already authorized selection, without another snapshot."""
    writer._rows_writable(manifest, "append" if candidate.name == "append_record" else candidate.arguments["operation"])
    if (views.store_identity(writer.connection) != candidate.store_identity
            or writer._container(row) != candidate.container_hash
            or manifest.manifest_version != candidate.manifest.manifest_version
            or writer._item(manifest.collection_id, candidate.key) != candidate.before
            or writer._held(manifest.collection_id, candidate.arguments["held"], corrections=True) != candidate.held_basis):
        raise connection.CollectionStoreError("COLLECTION_STORE_INPUT_CHANGED", "record candidate basis changed")
    if candidate.error is None:
        # Graph and policy reads can change without this collection's container changing.
        append = candidate.name == "append_record"
        fresh = writer._validate(manifest, declared, candidate.key, candidate.values,
            before=candidate.prior_values,
            operation="append" if append else candidate.arguments["operation"],
            written=None if append else candidate.changes)
        if fresh != candidate.values:
            raise connection.CollectionStoreError("COLLECTION_STORE_INPUT_CHANGED", "record candidate values changed")


def recheck_native(writer, candidate):
    for guard in candidate.guards:
        guard.recheck(writer.root)
    if candidate.adoption is not None and candidate.adoption.native is not None:
        observed = candidate.adoption
        fresh = edit_back.dependency_inputs(writer.root, [{"path": observed.path, "kind": "item"}],
            limit=1, byte_limit=record_formats._MAX_ITEM_BYTES)
        if fresh != observed.native:
            raise connection.CollectionStoreError("COLLECTION_STORE_INPUT_CHANGED", "detached source changed after observation")


def append(writer, row, manifest, declared, arguments):
    item, item_key, body = arguments["item"], arguments["item_key"], arguments["body"]
    held_basis = resumed = writer._held(manifest.collection_id, arguments["held"], corrections=True)
    if (resumed and resumed["kind"] == "view-correction" and manifest.storage.strategy == "markdown-log"
            and resumed["candidate"].get("action") == "view-edit"):
        resumed = {**resumed, "candidate": edit_back.resumed_append(writer, resumed, manifest)}
    if (item is None and resumed is None) or (item is not None and not isinstance(item, Mapping)):
        raise collections.CollectionError("INVALID_ITEM", "item must be an object")
    values = (records._apply_held_overrides(resumed["candidate"].get("item", {}), {} if item is None else item)
              if resumed else item)
    if resumed and resumed["candidate"]["action"] != "append":
        raise records._held_not_found()
    if resumed and resumed["kind"] == "view-correction" and resumed["code"] != edit_back.UNBOUND:
        raise records._held_not_found()
    if item_key is None and resumed and resumed["kind"] == "view-correction":
        item_key = resumed["candidate"].get("item_key")
    if body is None and resumed:
        body = resumed["candidate"].get("body")
    if body is None:
        body = ""
    records._validate_body(body)
    records._refuse_excluded_authored_names(values)
    if manifest.storage.strategy == "markdown-log" and body:
        raise collections.CollectionError("UNREPRESENTABLE_RECORD_BODY", "markdown-log storage cannot represent item bodies")
    if declared.kind == "intended":
        values = planning.normalize_item(values, vault_root=writer.root,
            validate_motivation=planning.motivation_is_governed(manifest))
    key = (records._validate_item_key(item_key, manifest=manifest, candidate=values) if item_key
           else collections.derived_item_key(manifest, values) or str(uuid.uuid4()))
    candidate = PreparedRecord("append_record", manifest.collection_id, arguments, manifest,
        views.store_identity(writer.connection), writer._container(row), key, values, body,
        writer._item(manifest.collection_id, key), resumed, held_basis, {})
    try:
        candidate.values = writer._validate(manifest, declared, key, values)
        candidate.body, _ = records._normalize_item_origin(writer.root, manifest, key=key, values=candidate.values,
            body=body, prior_body=None, item_path=lambda: None, render=lambda value: value,
            prepare_inputs=False, _canonical_owner=True)
        candidate.adoption = writer._admit_detached_resume(manifest, resumed)
    except collections.CollectionError as error:
        candidate.error = error
    candidate.hold_candidate = {"action": "append", "item": dict(candidate.values), "item_key": item_key,
        "body": candidate.body, "guards": {"expected_container_hash": arguments["expected_container_hash"]}}
    return candidate


def update(writer, row, manifest, declared, arguments):
    from .writer import _natural_key

    key = records._validate_item_key(arguments["item_key"], manifest=manifest, candidate=arguments["changes"])
    before = writer._item(manifest.collection_id, key)
    if before is None:
        raise collections.CollectionError("RECORD_NOT_FOUND", "record key does not exist")
    resumed = writer._held(manifest.collection_id, arguments["held"], corrections=True)
    changes, delete_fields, body = arguments["changes"], arguments["delete_fields"], arguments["body"]
    operation, refresh = arguments["operation"], arguments["refresh_presentation"]
    if resumed:
        value = resumed["candidate"]
        if resumed["kind"] == "view-correction":
            value = edit_back.resumed_update(writer, resumed, manifest, before)
        if value["action"] != "update" or value["item_key"] != key:
            raise records._held_not_found()
        if changes is not None and not isinstance(changes, Mapping):
            raise collections.CollectionError("INVALID_RECORD_CHANGES", "changes must be a non-empty object")
        changes = records._apply_held_overrides(value.get("changes", {}), {} if changes is None else changes)
        delete_fields = delete_fields or tuple(value.get("delete_fields", ()))
        body = value.get("body") if body is None else body
    if not isinstance(changes, Mapping) or (not changes and not delete_fields and body is None and not refresh):
        raise collections.CollectionError("INVALID_RECORD_CHANGES", "changes must be a non-empty object")
    if refresh and manifest.record_presentation is None and manifest.item_presentation is None:
        raise collections.CollectionError("INVALID_RECORD_PRESENTATION", "collection has no presentation recipe")
    records._refuse_excluded_authored_names(changes)
    old_values = json.loads(before["values_json"])
    final = dict(old_values)
    if declared.kind == "intended":
        if operation == "triage":
            # Planning fixes the triage fields and nullable relation fields in its public protocol.
            if (not changes or set(changes) - planning.TRIAGE_FIELDS
                    or any(value is None and name not in {"area", "parent"} for name, value in changes.items())):
                raise collections.CollectionError("INVALID_PLAN_ARGUMENTS", "triage transition is invalid")
            if old_values["lifecycle"] != "active" or old_values["kind"] == "area":
                raise collections.CollectionError("INVALID_PLAN", "triage requires an active deliverable plan")
        changes, deleted = planning._normalize_changes(manifest, changes)
        delete_fields = tuple(set(delete_fields) | set(deleted))
    for name in delete_fields:
        final.pop(name, None)
    final.update(changes)
    final_body = before["body"] if body is None else body
    records._validate_body(final_body)
    if declared.kind == "intended" and final == old_values and final_body == before["body"]:
        raise collections.CollectionError("INVALID_PLAN", "Planning update does not change authored state")
    records._expect_hash(arguments["expected_container_hash"], writer._container(row), "container")
    records._expect_hash(arguments["expected_item_version"], writer._version(before), "item")
    candidate = PreparedRecord("update_record", manifest.collection_id, arguments, manifest,
        views.store_identity(writer.connection), writer._container(row), key, final, final_body, before,
        resumed, resumed, {}, changes=dict(changes), delete_fields=tuple(delete_fields), prior_values=old_values,
        supplied_body=body)
    try:
        candidate.values = writer._validate(manifest, declared, key, final, before=old_values, operation=operation,
                                            written=changes)
    except collections.CollectionError as error:
        if resumed and resumed["kind"] == "view-correction":
            raise
        candidate.error = error
    else:
        if refresh and not changes and not delete_fields and body is None:
            projection = writer.connection.execute(
                "SELECT COALESCE(pending_sha256,published_sha256) FROM projection_state WHERE path=?",
                (before["view_path"],)).fetchone()
            with views.presentation_resolver(writer.connection, writer.root, manifest, operation=writer._operation,
                    observe_read=candidate.guards.append) as resolver:
                rendered = record_formats.render_markdown_item(manifest, candidate.values, key, final_body,
                    resolve_relationship=resolver, view_stamp=views.stamp(candidate.store_identity,
                        before["row_version"], before["payload_hash"]))
            if projection and projection[0] == hashlib.sha256(rendered.encode()).hexdigest():
                raise collections.CollectionError("NOOP_RECORD_PRESENTATION", "managed presentation is already current")
        writer._twins(manifest.collection_id, key, _natural_key(manifest, candidate.values))
        candidate.body, _ = records._normalize_item_origin(writer.root, manifest, key=key, values=candidate.values,
            body=final_body, prior_body=before["body"], item_path=lambda: None, render=lambda value: value,
            prepare_inputs=False, _canonical_owner=True)
    candidate.hold_candidate = {"action": "update", "item_key": key, "changes": dict(changes),
        "delete_fields": list(delete_fields), "body": body,
        "guards": {"expected_container_hash": arguments["expected_container_hash"],
                   "expected_item_version": arguments["expected_item_version"]}}
    return candidate
