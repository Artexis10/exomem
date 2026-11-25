"""Single product-command dispatch for human-owned structured Records."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Never

from . import (
    automatic_collection,
    collection_refs,
    kbdir,
    operation_entries,
    query_data,
    record_governance,
    record_history,
    records,
    structured_collections,
    writer_lease,
)
from .cli_ops import OpError, action_argument_contract
from .query_compat import public_filter_annotation
from .structured_collections import CollectionError, collection_remediation

_QueryFilters = public_filter_annotation()

# The operation entries are the one table of what each action accepts and requires.
ACTIONS = operation_entries.RECORDS.actions()
_ACTION_FIELDS = operation_entries.RECORDS.action_fields()
_REQUIRED_FIELDS = operation_entries.RECORDS.required_fields()
_QUERY_SHAPING_FIELDS = frozenset(
    {
        "filters",
        "columns",
        "sort_by",
        "descending",
        "limit",
        "aggregate",
        "date_from",
        "date_to",
        "date_column",
        "expand_children",
        "expand_child",
    }
)


def record_memory(
    vault_root: Path,
    action: Literal[
        "describe",
        "validate",
        "inspect",
        "create",
        "query",
        "append",
        "bulk_upsert",
        "update",
        "retract",
        "revise",
        "rebaseline",
        "discard",
        "import",
        "history",
    ],
    collection: str | None = None,
    manifest_path: str | None = None,
    manifest_text: str | None = None,
    why: str | None = None,
    scaffold: bool | None = None,
    view: str | None = None,
    filters: _QueryFilters = None,
    columns: list[str] | None = None,
    sort_by: str | None = None,
    descending: bool | None = None,
    limit: int | None = None,
    aggregate: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    date_column: str | None = None,
    expand_children: bool | None = None,
    expand_child: str | None = None,
    continuation: str | None = None,
    include_agent_history: bool | None = None,
    output_format: Literal["json", "markdown", "csv"] | None = None,
    item: dict[str, Any] | None = None,
    item_key: str | None = None,
    expected_container_hash: str | None = None,
    expected_manifest_hash: str | None = None,
    acknowledged_gap_codes: list[str] | None = None,
    body: str | None = None,
    delivery: records.ArtifactDelivery | None = None,
    changes: dict[str, Any] | None = None,
    expected_item_version: str | None = None,
    refresh_presentation: bool | None = None,
    held: str | None = None,
    hold: bool | None = None,
    rows: list[dict[str, Any]] | None = None,
    source: str | None = None,
    on_reject: Literal["abort", "skip"] | None = None,
    import_request: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    item_id: str | None = None,
    automatic: bool | None = None,
    new_collection: dict[str, Any] | None = None,
    new_fields: list[str] | None = None,
    natural_key: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Describe, validate, inspect, create, query, append, bulk_upsert, update, retract, revise, rebaseline, discard or import Records.

    Records are human-owned event and state histories.  This command keeps the
    complete workflow on one product surface while routing mutations to guarded
    writers and reads through governance-aware projections.

    Args:
        action: describe, validate, inspect, create, query, append, bulk_upsert, update,
            retract, revise, rebaseline, or discard.
        collection: Vault-relative manifest path, collection UUID or memory reference.
            Omit for inventory inspect; required for targeted reads and writes.
        manifest_path: Proposed manifest path for validate or create.
        manifest_text: Complete proposed manifest text for validate or create.
        why: Concise audit reason for create, append, update, or discard.
        scaffold: Create an initial canonical source for create; defaults to true.
        view: Saved query view for query; cannot be combined with inline shaping.
        filters: Query predicates.
        columns: Query columns.
        sort_by: Query sort column.
        descending: Sort descending for query.
        limit: Bounded query result limit.
        aggregate: Optional aggregate for query.
        date_from: Inclusive query date lower bound.
        date_to: Inclusive query date upper bound.
        date_column: Query date property.
        expand_children: Expand the one unambiguous child container for backward compatibility.
        expand_child: Exact declared child table/container to project and expand.
        continuation: Snapshot-bound query continuation.
        include_agent_history: Include bounded governed agent mutation history.
        output_format: Query output format.
        item: Item values for append.
        item_key: The item's internal UUID identity, required for update. Omit it
            on append and identity derives from the collection's declared natural
            key; supplying a natural-key value here refuses. On query it selects
            that one row and returns its Markdown body as `body`.
        expected_container_hash: Exact current container hash for append or update.
        body: Optional Markdown item body for append.
        delivery: Optional receipt-gated artifact-delivery validation envelope
            for append. The caller still authors every mapped item field.
        changes: Targeted changes for update.
        expected_item_version: Exact current item version for update.
        refresh_presentation: Guardedly rebuild the managed Markdown presentation during update.
        held: Reference to a held candidate. On append or update it resumes that
            candidate, with `item` or `changes` supplying optional overrides and a
            null value removing a field; on discard it names the candidate to remove.
        hold: Set false to refuse an invalid candidate without holding it. A refused
            append or update otherwise preserves the complete candidate as a held
            file under the collection and returns its reference beside the refusal.
        rows: bulk_upsert only: 1 to 50 objects of `item`, optional `body` and
            `source`, committed under ONE `expected_container_hash`.
        source: bulk_upsert only: the path of a preserved Sources or Evidence page.
            Required once for the batch or on each row; a row's own `source` wins.
        on_reject: bulk_upsert only: `abort` (default) writes nothing when any row
            is rejected and reports every would-be outcome; `skip` commits the rest.
        import_request: import only, for a store-routed collection: a durable job
            streams one preserved Sources/Evidence file in bounded batches; store-mode
            `describe` carries its contract.
        query: query only, for a store-routed collection: a typed v1 query object
            instead of the legacy shaping arguments; store-mode `describe` names its
            grammar route.
        item_id: update, retract or history: the row `id` a query returned. On update and retract it
            stands for item_key, expected_item_version and expected_container_hash, which the call
            reads under the writer lease, and a stale id is refused with the current one. On history
            it names the row whatever version the id carries.
        natural_key: history only: the values of the collection's natural-key fields, as the row was
            written, e.g. {"asset_tag": "L-17"}. A retracted row is found by the natural key it was created with, or by item_key.
            History takes exactly one of item_key, item_id and natural_key.
        automatic: create only: true when you start the collection yourself under silent
            proactive_capture. Unless the bounds in `automatic_collection` hold, the server refuses
            AUTOMATIC_CREATION_REFUSED and names the failed bound; nothing is created. The receipt
            and the inventory then report the collection as automatic.
        new_collection: append only, `{"natural_key": [<item fields that identify a row>]}`: when
            `collection` is a title that names no collection, create a minimal one from `item` through
            the same automatic create (so its bounds hold) and append the row, in one call with one
            receipt. A title that names a collection is a plain append. A refusal creates nothing and
            carries the manifest it would have created in its details.
        new_fields: append or update only, the fields of `item` (append) or `changes` (update) that the
            collection does not declare yet. Under silent proactive_capture the server adds them, typed
            from the values, then writes the row, in one call; the reply names them in `added_fields`.
            Under advisory or off, or past the field bound, it refuses AUTOMATIC_FIELDS_REFUSED with the
            proposal. A declared field is never retyped, renamed or removed this way.

    retract takes one row out of current state with a required why. Its history stays, and
    appending the same natural key later brings the row back. A store-routed collection
    without one view file per row takes it; any other refuses RETRACT_UNAVAILABLE.
    """
    collection = collection_refs.resolve_collection(vault_root, collection, profile="records")
    if item_id is not None:
        reads_history = action == "history"
        if reads_history:
            _require_one_history_selector({"item_key": item_key, "item_id": item_id, "natural_key": natural_key})
        elif action not in {"update", "retract"} or any(
                v is not None for v in (item_key, expected_container_hash, expected_item_version)):
            _invalid_arguments("item_id applies to update, retract or history and replaces item_key and both guards")
        from .collection_store import preview

        # A reader guards no write, so the id names its row at any version.
        bound = preview.bind_row(vault_root, str(collection), item_id, stale_ok=reads_history) or collection_refs.bind_item(
            lambda **kw: record_memory(vault_root, "query", **kw),
            collection=str(collection), item_id=item_id, profile="records", stale_ok=reads_history,
        )
        item_key = bound["item_key"]
        if not reads_history:
            expected_item_version, expected_container_hash = (
                bound["expected_item_version"], bound["expected_container_hash"],
            )
    values = {
        "collection": collection,
        "manifest_path": manifest_path,
        "manifest_text": manifest_text,
        "why": why,
        "scaffold": scaffold,
        "view": view,
        "filters": filters,
        "columns": columns,
        "sort_by": sort_by,
        "descending": descending,
        "limit": limit,
        "aggregate": aggregate,
        "date_from": date_from,
        "date_to": date_to,
        "date_column": date_column,
        "expand_children": expand_children,
        "expand_child": expand_child,
        "continuation": continuation,
        "include_agent_history": include_agent_history,
        "output_format": output_format,
        "item": item,
        "item_key": item_key,
        "expected_container_hash": expected_container_hash,
        "expected_manifest_hash": expected_manifest_hash,
        "acknowledged_gap_codes": acknowledged_gap_codes,
        "body": body,
        "delivery": delivery,
        "changes": changes,
        "expected_item_version": expected_item_version,
        "refresh_presentation": refresh_presentation,
        "held": held,
        "hold": hold,
        "rows": rows,
        "source": source,
        "on_reject": on_reject,
        "import_request": import_request,
        "query": query,
        "automatic": automatic,
        "new_collection": new_collection,
        "new_fields": new_fields,
        "natural_key": natural_key,
    }
    _validate_arguments(action, values)
    # A leaf takes only the arguments it declares, and these two are the facade's alone.
    del values["new_collection"], values["new_fields"]
    if action in {"update", "retract"} and None in (item_key, expected_container_hash, expected_item_version):
        _invalid_arguments(f"{action} requires item_id, or item_key with expected_container_hash and expected_item_version")
    from .query_engine.errors import QueryError

    try:
        from .collection_store import preview

        if action == "create" and automatic:
            automatic_collection.require_bounds(vault_root, manifest_path, manifest_text)
        if new_fields is not None:
            return _with_new_fields(vault_root, action, values, new_fields, new_collection)
        if automatic_collection.creates_from_row(action, collection, new_collection):
            return _append_creating(vault_root, values, new_collection)
        selected, result = preview.dispatch(vault_root, "records", action, values)
        if selected:
            return result
        if action in {"import", "retract"} or query is not None or (action == "query" and item_key is not None):
            # Resolve under the caller's authority first: an absent or withheld selector
            # answers exactly as a sealed store-routed collection does, so the refusal
            # below never confirms that a collection exists.
            assert collection is not None
            record_governance.resolve_collection(vault_root, collection)
            if action == "import":
                raise CollectionError(
                    "IMPORT_UNAVAILABLE", "import runs only for a store-routed Records collection"
                )
            if action == "retract":
                raise CollectionError(
                    "RETRACT_UNAVAILABLE", "retract runs only for a store-routed Records collection"
                )
            if query is None:
                raise CollectionError(
                    "QUERY_UNAVAILABLE", "a query with item_key reads a store-routed collection; this one keeps "
                    "each body in its item file, so read that file with read_memory"
                )
            raise CollectionError(
                "QUERY_UNAVAILABLE", "a v1 query runs only on a store-routed Records collection"
            )
        if action == "describe":
            from .collection_store import ownership

            return parse_manifest_contract(store_vault=not ownership.file_only(vault_root))
        if action == "validate":
            assert manifest_text is not None
            if collection is not None:
                return records.validate_collection_revision(vault_root, collection, manifest_text)
            assert manifest_path is not None
            return records.validate_collection_create(
                vault_root,
                manifest_path,
                manifest_text,
                scaffold=True if scaffold is None else scaffold,
            )
        if action == "inspect":
            if collection is None:
                return record_governance.inventory_collections(vault_root)
            if _is_direct_legacy_tracker_selector(collection):
                return record_governance.inspect_legacy_tracker(vault_root, collection)
            return record_governance.inspect_collection(vault_root, collection)
        if action == "create":
            assert manifest_path is not None
            assert manifest_text is not None
            assert why is not None
            root = Path(vault_root)
            record_governance.require_candidate_manifest_visibility(root, manifest_path)
            manifest = structured_collections.parse_manifest_bytes(
                root, root / manifest_path, manifest_text.encode("utf-8")
            )
            record_governance.require_records_profile(manifest)
            records.require_file_create(root, manifest)
            return records.create_collection(
                vault_root,
                manifest_path,
                manifest_text,
                why=why,
                scaffold=True if scaffold is None else scaffold,
                automatic=bool(automatic),
            )
        if action == "query":
            assert collection is not None
            manifest = record_governance.require_records_profile(
                record_governance.resolve_collection(vault_root, collection)
            )
            result = record_governance.query_collection(
                vault_root,
                manifest,
                view=view,
                filters=filters,
                columns=columns,
                sort_by=sort_by,
                descending=False if descending is None else descending,
                limit=query_data.DEFAULT_LIMIT if limit is None else limit,
                aggregate=aggregate,
                date_from=date_from,
                date_to=date_to,
                date_column=date_column,
                expand_children=False if expand_children is None else expand_children,
                expand_child=expand_child,
                continuation=continuation,
            )
            history = None
            if include_agent_history is True:
                history = records.agent_audit_history(
                    vault_root,
                    manifest,
                    authorize_path=record_governance.full_release_filter(vault_root),
                )
            return collection_refs.mark_rows(record_governance.project_query_result(
                result,
                manifest,
                output_format="json" if output_format is None else output_format,
                agent_history=history,
            ), profile="records")
        if action == "history":
            assert collection is not None
            return record_history.read(
                vault_root,
                collection,
                item_key=item_key,
                natural_key=natural_key,
                semantic_profile="records",
                limit=limit,
                continuation=continuation,
            )
        if action == "append":
            assert collection is not None
            assert why is not None
            manifest = record_governance.require_records_profile(
                record_governance.resolve_collection_for_mutation(vault_root, collection)
            )
            append_kwargs: dict[str, Any] = {
                "item": item,
                "item_key": item_key,
                "expected_container_hash": expected_container_hash,
                "why": why,
                "held": held,
                "hold": True if hold is None else hold,
            }
            # An omitted body leaves a resumed candidate's own body in place.
            if body is not None:
                append_kwargs["body"] = body
            if delivery is not None:
                append_kwargs["delivery"] = delivery
            return records.append_record(vault_root, manifest, **append_kwargs)
        if action == "bulk_upsert":
            assert collection is not None
            assert why is not None
            assert rows is not None
            assert expected_container_hash is not None
            manifest = record_governance.require_records_profile(
                record_governance.resolve_collection_for_mutation(vault_root, collection)
            )
            return records.bulk_upsert_records(
                vault_root,
                manifest,
                rows=rows,
                why=why,
                expected_container_hash=expected_container_hash,
                source=source,
                on_reject="abort" if on_reject is None else on_reject,
            )
        if action == "discard":
            assert collection is not None
            assert held is not None
            assert why is not None
            manifest = record_governance.require_records_profile(
                record_governance.resolve_collection_for_mutation(vault_root, collection)
            )
            return records.discard_held(vault_root, manifest, held=held, why=why)
        if action == "revise":
            assert collection is not None
            assert manifest_text is not None
            assert expected_manifest_hash is not None
            assert expected_container_hash is not None
            assert why is not None
            return records.revise_collection(
                vault_root,
                collection,
                manifest_text=manifest_text,
                expected_manifest_hash=expected_manifest_hash,
                expected_container_hash=expected_container_hash,
                why=why,
            )
        if action == "rebaseline":
            assert collection is not None
            assert expected_manifest_hash is not None
            assert expected_container_hash is not None
            assert acknowledged_gap_codes is not None
            assert why is not None
            return records.rebaseline_collection(
                vault_root,
                collection,
                expected_manifest_hash=expected_manifest_hash,
                expected_container_hash=expected_container_hash,
                acknowledged_gap_codes=acknowledged_gap_codes,
                why=why,
            )
        assert collection is not None
        assert item_key is not None
        assert changes is not None or refresh_presentation is True or held is not None
        assert expected_container_hash is not None
        assert expected_item_version is not None
        assert why is not None
        manifest = record_governance.require_records_profile(
            record_governance.resolve_collection_for_mutation(vault_root, collection)
        )
        return records.update_record(
            vault_root,
            manifest,
            item_key=item_key,
            changes={} if changes is None else changes,
            expected_container_hash=expected_container_hash,
            expected_item_version=expected_item_version,
            why=why,
            refresh_presentation=refresh_presentation is True,
            held=held,
            hold=True if hold is None else hold,
        )
    except (CollectionError, query_data.QueryDataError) as error:
        details = _json_safe_details(getattr(error, "details", None))
        remediation = collection_remediation(error) if isinstance(error, CollectionError) else None
        reason = error.reason
        if isinstance(error, CollectionError) and error.code == "SCHEMA_UNKNOWN_FIELD" and action in {"append", "update"}:
            details, reason = _unknown_field_route(vault_root, action, values, details or {}, reason)
        raise OpError(error.code, reason, remediation, details=details) from error
    except QueryError as error:
        from .query_engine import route

        refusal = route.details(error)
        raise OpError(error.code, error.message, refusal["repair"], details=refusal) from error


def _append_creating(vault_root: Path, values: dict[str, Any], declared: dict[str, Any]) -> dict[str, Any]:
    """`add_record` with `new_collection` for a title that names no collection: create it from the row, then append.

    Both steps are the ordinary commands: the automatic create, whose bounds all hold, and the append.
    The row is checked against the drafted collection first, so a refused row leaves no empty collection.
    The create takes a derived request identity, because the store keeps one transaction per identity and the
    append keeps the call's own. A call that fails after the create is reported committed-uncertain by the writer
    lease, and its retry finds the collection and appends the row.
    """
    for name in ("held", "expected_container_hash", "delivery", "item_key"):
        if values[name] is not None:
            _invalid_arguments(f"new_collection starts the collection with this row, so it takes no {name}")
    natural_key = declared["natural_key"] if isinstance(declared, Mapping) and set(declared) == {"natural_key"} else None
    if not (
        isinstance(natural_key, list) and natural_key and len(set(natural_key)) == len(natural_key)
        and all(isinstance(name, str) and name for name in natural_key)
    ):
        _invalid_arguments('new_collection is {"natural_key": [one or more distinct fields of item]}')
    root = Path(vault_root)
    title = str(values["collection"]).strip()
    item, body = values["item"], values["body"]
    if not isinstance(item, Mapping):
        raise CollectionError("INVALID_ITEM", "item must be an object")
    draft = automatic_collection.minimal_manifest(root, title, item, natural_key)
    manifest = structured_collections.parse_manifest_bytes(
        root, root / draft.manifest_path, draft.manifest_text.encode("utf-8")
    )
    records.validate_new_row(manifest, item, body=body)
    try:
        with writer_lease.mutation_step("create"):
            created = record_memory(
                vault_root, "create", manifest_path=draft.manifest_path, manifest_text=draft.manifest_text,
                why=values["why"], automatic=True,
            )
    except OpError as refusal:
        if refusal.code != "AUTOMATIC_CREATION_REFUSED":
            raise
        # The confirmed create is the call that can succeed: it carries the manifest this call would have created.
        retry_call = {
            "action": "create", "manifest_path": draft.manifest_path, "manifest_text": draft.manifest_text,
            "why": values["why"],
        }
        raise OpError(
            refusal.code, refusal.message, refusal.remediation, details={**refusal.details, "retry_call": retry_call}
        ) from refusal
    made = {
        "collection_id": created["collection_id"], "title": title, "manifest_path": draft.manifest_path,
        "natural_key": list(draft.natural_key), "fields": list(draft.fields),
    }
    appended = record_memory(
        vault_root, "append", collection=draft.manifest_path, item=item, why=values["why"], body=body,
        hold=values["hold"],
    )
    warnings = [*created.get("warnings", ()), *appended.get("warnings", ())]
    return {**appended, "automatic": True, "created_collection": made, **({"warnings": warnings} if warnings else {})}


def _current_manifest(
    vault_root: Path, collection: Any
) -> tuple[structured_collections.CollectionManifest, str, bool | None]:
    """The Records collection `collection` names, with its manifest text, from the store that owns it or from its file.

    A store collection also reports whether its create transaction was automatic, read on the store thread with
    the manifest; None marks a file collection, whose create record `automatic_collection` reads when it needs it.
    """
    from .collection_store import preview

    selected, found = preview.dispatch(vault_root, "records", "manifest_text", {"collection": collection})
    if selected:
        return found
    manifest = record_governance.require_records_profile(
        record_governance.resolve_collection_for_mutation(vault_root, collection)
    )
    return manifest, records.manifest_text(vault_root, manifest), None


def _field_revision(
    vault_root: Path, manifest: structured_collections.CollectionManifest, text: str, added: Mapping[str, str], why: str
) -> dict[str, Any]:
    """The `revise` that adds `added` to the collection: the confirmed route to the same change, with its guards read now."""
    revised = automatic_collection.with_added_fields(text, added)
    guards = record_memory(vault_root, "validate", collection=manifest.collection_id, manifest_text=revised)
    return {"collection": manifest.collection_id, "manifest_text": revised, **guards["lifecycle_guards"], "why": why}


def _field_refusal_details(
    vault_root: Path, manifest: structured_collections.CollectionManifest, text: str, added: Mapping[str, str],
    why: str, refusal: CollectionError,
) -> dict[str, Any]:
    """The details of a refused automatic addition: the failed bound, the proposal, and the confirmed `revise` for it.

    A revise that cannot be drafted leaves the proposal standing and names why in `retry_unavailable`.
    """
    details = {**refusal.details, "proposal": {"collection": manifest.title, "add_fields": added}}
    try:
        return {**details, "retry_call": {"action": "revise", **_field_revision(vault_root, manifest, text, added, why)}}
    except CollectionError as unavailable:
        return {**details, "retry_unavailable": unavailable.reason}
    except OpError as unavailable:
        return {**details, "retry_unavailable": unavailable.message}


def _with_new_fields(
    vault_root: Path, action: str, values: dict[str, Any], names: Any, new_collection: Any
) -> dict[str, Any]:
    """`add_record` or `edit_record` with `new_fields`: add the fields the call names, typed from its values, then run the write.

    The addition is the ordinary `revise` (its guards, its audit event, the store's own writer for a store
    collection), under the bounds of `automatic_collection`. A field the collection already declares adds nothing, and
    the row's own validation judges its value, so a replay of the call, or a second call with the same row, writes only the row. The fields
    commit before the row, and a failure of the row leaves them declared; the retry adds nothing twice.
    """
    carried_key = "item" if action == "append" else "changes"
    carried = values[carried_key]
    if new_collection is not None:
        _invalid_arguments("new_fields adds fields to an existing collection, and new_collection starts one")
    if not (
        isinstance(names, list) and names and len(set(names)) == len(names)
        and all(isinstance(name, str) and name for name in names)
    ):
        _invalid_arguments("new_fields is a list of distinct field names")
    if not isinstance(carried, Mapping) or [name for name in names if name not in carried]:
        _invalid_arguments(f"new_fields names fields that {carried_key} does not carry")
    manifest, text, store_created = _current_manifest(vault_root, values["collection"])
    added = automatic_collection.fields_to_add(manifest, {name: carried[name] for name in names})
    if added:
        try:
            automatic_collection.require_field_bounds(vault_root, manifest, added, store_created=store_created)
        except CollectionError as refusal:
            raise CollectionError(
                refusal.code, refusal.reason,
                _field_refusal_details(vault_root, manifest, text, added, values["why"], refusal),
            ) from refusal
        revision = _field_revision(vault_root, manifest, text, added, values["why"])
        with writer_lease.mutation_step("fields"):
            revised = record_memory(vault_root, "revise", **revision)
        # The revise moved the container hash. A caller's guard that named the hash it revised from now names the
        # revised one, and a guard from an older state stays and refuses the write as stale.
        if values["expected_container_hash"] == revision["expected_container_hash"]:
            values = {**values, "expected_container_hash": revised["after_container_hash"]}
    reply = record_memory(vault_root, action, **values)
    return {**reply, "automatic": True, "added_fields": added} if added else reply


def _unknown_field_route(
    vault_root: Path, action: str, values: dict[str, Any], details: dict[str, Any], reason: str
) -> tuple[dict[str, Any], str]:
    """The refusal of an undeclared field, with the next step for the disposition it was refused under.

    Only a refusal whose every issue is an undeclared field has one: the call can succeed with `new_fields`
    under silent capture, and otherwise the agent proposes the field to the user, with the confirmed `revise` ready.
    The held candidate keeps the row meanwhile, so the retry resumes it. Any other refusal is returned as it is.
    """
    issues = details.get("issues") or []
    names = [issue.get("field") for issue in issues]
    carried = values["item" if action == "append" else "changes"]
    if not names or any(issue.get("code") != "SCHEMA_UNKNOWN_FIELD" for issue in issues) or not isinstance(carried, Mapping):
        return details, reason
    if any(name not in carried for name in names):
        # The undeclared field came from a held candidate, whose values this call does not carry.
        return details, reason
    try:
        manifest, text, store_created = _current_manifest(vault_root, values["collection"])
        added = automatic_collection.fields_to_add(manifest, {name: carried[name] for name in names})
        try:
            automatic_collection.require_field_bounds(vault_root, manifest, added, store_created=store_created)
            # A layout that `new_fields` cannot extend in place refuses that retry too, so it is not offered.
            automatic_collection.with_added_fields(text, added)
        except CollectionError as refusal:
            kept = {key: value for key, value in details.items() if key != "retry_with"}
            fields = _field_refusal_details(vault_root, manifest, text, added, values["why"], refusal)
            return {**kept, **fields}, f"{reason}: {refusal.reason}"
    except (CollectionError, OpError):
        # The original refusal stands: this only adds a next step, and a manifest it cannot read gives none.
        return details, reason
    held = details.get("held")
    resume = {"held": held["held_id"]} if isinstance(held, Mapping) and "held_id" in held else {}
    return {**details, "retry_with": {"new_fields": names, **resume}}, reason


def parse_manifest_contract(*, store_mode: bool = False, store_vault: bool = False) -> dict[str, Any]:
    """Project the parser-owned collection contract without vault content.

    A bound store (``store_mode``) or a vault with an enrolled store (``store_vault``) creates
    only sqlite collections, so its examples declare sqlite.
    """
    from .collection_store.connection import UNSUPPORTED_WRITERS
    from .structured_collections import manifest_authoring_contract

    # A served vault answers describe in file mode too, so both modes carry the store's writer rule.
    return {
        **manifest_authoring_contract(store=store_mode or store_vault),
        "bulk_upsert": _bulk_upsert_contract(store_mode=store_mode),
        "history": _history_contract(),
        "writer_lease": UNSUPPORTED_WRITERS,
    }


def _history_contract() -> dict[str, Any]:
    """The `history` action's contract, from the selectors its operation entry accepts."""
    return {
        "why": "read one row's retained revisions, newest first, each with its reason",
        "select_by": "exactly one of " + ", ".join(operation_entries.HISTORY_SELECTORS),
        "arguments": {
            "item_key": "the row's internal UUID identity",
            "item_id": "the row `id` that a query returned; a stale id still names the row",
            "natural_key": (
                "an object with a value for each of the collection's natural-key fields, and no other "
                "field, as the row was written"
            ),
        },
        "retracted": (
            "item_key finds a retracted row; natural_key finds it only by the key its identity "
            "derives from (the key it was created with, unless an item_key was given); item_id "
            "names only a current row"
        ),
        "refusal": (
            "by item_key or natural_key: RECORD_NOT_FOUND, alike for a row that does not exist, a row "
            "the caller may not read, and a natural key with a field the caller may not read; by "
            "item_id: the row binder's refusals (ITEM_NOT_FOUND and its siblings), alike for an "
            "absent and a hidden row"
        ),
        "example": {
            "action": "history",
            "collection": kbdir.managed_join("Records/Publications/_collection.md"),
            "natural_key": {"published_on": "2026-09-01", "slug": "entry-one"},
        },
    }


def _bulk_upsert_contract(*, store_mode: bool = False) -> dict[str, Any]:
    """The `bulk_upsert` action's contract, from the writer's own constants."""
    contract = {
        "why": (
            "load many already-normalised rows under ONE container-hash guard instead of "
            "one guarded append per row, whose changing hash forces serial writes"
        ),
        "arguments": {
            "collection": "the Records collection",
            "rows": (
                f"1 to {records.BULK_UPSERT_MAX_ROWS} objects of item, optional body and "
                "source per call (source is required once for the batch or on each row); "
                "send a larger import as repeated calls, each "
                "chained from the previous response's after_container_hash"
            ),
            "expected_container_hash": "required; compared once for the whole batch",
            "why": "one audit reason for the batch",
            "source": (
                "required once for the batch or on each row: the vault path of a preserved "
                "Sources or Evidence page; a row's own source overrides the batch's"
            ),
            "on_reject": "abort (default) or skip",
        },
        "outcomes": {
            "inserted": "no item holds the row's identity",
            "updated": (
                "an item holds the identity with different values; its values are replaced "
                "wholesale and its body only when the row supplies one. Unlike append, a "
                "changed payload for a held identity is not a conflict"
            ),
            "unchanged": "an item holds the identity with identical values; nothing is written",
            "rejected": "code and field paths; the row is never written",
        },
        "identity": (
            "a row with a complete declared natural key derives its identity from it, so a "
            "re-stated row is unchanged or updated. A row without one gets a generated "
            "identity and can only be inserted or rejected: re-running the same rows "
            "duplicates them. A repeated natural key inside one request is rejected "
            "DUPLICATE_ROW_KEY"
        ),
        "abort_and_skip": (
            "abort writes nothing when any row is rejected and still reports every row's "
            "would-be outcome; skip publishes the accepted rows in one atomic write. "
            "`committed` says which happened"
        ),
        "provenance": (
            "every row must resolve to a preserved Sources or Evidence page the caller may "
            "read; a missing, non-preserved or withheld page is rejected identically "
            "(SOURCE_NOT_FOUND). A declared array-of-link `sources` field receives the "
            "verified reference; otherwise it rides each event's audit rationale. For a "
            "collection with a `sources` field, a row that omits `sources` gets the call's "
            "source link, so re-importing unchanged values from a new source reports every "
            "row `updated` and replaces the link"
        ),
        "audit": (
            "the audit protocol is unchanged: each written row gets an ordinary chained "
            "append or update event, published in one atomic write with one combined log "
            "write. The response carries one batch receipt (batch_id, counts, per-row "
            "outcomes with transition ids). Events and the receipt never carry row values"
        ),
        "limits": {
            "max_rows": records.BULK_UPSERT_MAX_ROWS,
            "max_rows_why": (
                "one call holds the single writer lease for its whole publish, and that hold "
                "grows faster than the row count, so the cap keeps one call near 5 seconds "
                "and never blocks every other write for a minute"
            ),
            "target_rows": records.BULK_UPSERT_TARGET_ROWS,
            "target_rows_note": (
                "the intended per-call size once collections are SQLite-authoritative; "
                "until then a client imports in repeated calls of at most max_rows"
            ),
            "audit_chain_depth_budget": records._MAX_AUDIT_CHAIN_DEPTH,
            "depth_rule": (
                f"each written row consumes one of the collection's {records._MAX_AUDIT_CHAIN_DEPTH} "
                "audit-chain events; a batch that would cross the budget refuses up front "
                "with BULK_UPSERT_AUDIT_DEPTH and writes nothing; when the log history cannot "
                "be fully read the count is unknown, the call proceeds, and the result carries "
                'depth_check "unknown" with depth_check_reason "history_incomplete"'
            ),
            "refusals": [
                "STALE_RECORD",
                "BULK_UPSERT_TOO_MANY_ROWS",
                "BULK_UPSERT_EMPTY",
                "BULK_UPSERT_AUDIT_DEPTH",
                "COLLECTION_ITEM_LIMIT",
            ],
        },
        "retry": (
            "a retry under the same transport idempotency identity returns the recorded "
            "result. On a natural-keyed row a retry without one is safe: committed rows are "
            "unchanged. Generated-identity rows have no content replay, so rely on the "
            "transport identity there"
        ),
        "example": {
            "action": "bulk_upsert",
            "collection": "Knowledge Base/Records/Publications/_collection.md",
            "why": "import a normalised export",
            "expected_container_hash": "<hash from inspect or the previous response>",
            "source": "Knowledge Base/Evidence/export-2026-09.md",
            "rows": [
                {"item": {"published_on": "2026-09-01", "slug": "entry-one"}},
                {"item": {"published_on": "2026-09-02", "slug": "entry-two"}, "body": "Notes."},
            ],
        },
    }
    if store_mode:
        from .collection_store.writer import BULK_UPSERT_MAX_ROWS

        contract["arguments"]["rows"] = (
            f"1 to {BULK_UPSERT_MAX_ROWS} objects of item, optional body and source per call "
            "(source is required once for the batch or on each row); "
            "chain larger imports from the previous response's after_container_hash"
        )
        contract["audit"] = (
            "one transaction and one transition with an effect per written row; first_transition "
            "equals last_transition, and every changed row carries that transition_id. "
            "Events and the receipt never carry row values"
        )
        contract["provenance"] = (
            "every row must resolve to a preserved Sources or Evidence page the caller may "
            "read; missing, non-preserved and withheld pages reject identically (SOURCE_NOT_FOUND). "
            "The verified reference is preserved with the item version and a declared array-of-link "
            "sources field receives it using the same replacement semantics as file mode"
        )
        limits = contract["limits"]
        limits["max_rows"] = BULK_UPSERT_MAX_ROWS
        limits["max_rows_why"] = "the store commits all accepted rows in one SQLite transaction"
        limits["target_rows_note"] = "the store's current per-call cap"
        limits.pop("audit_chain_depth_budget")
        limits.pop("depth_rule")
        limits["refusals"].remove("BULK_UPSERT_AUDIT_DEPTH")
        limits["refusals"].remove("COLLECTION_ITEM_LIMIT")
    return contract


def _json_safe_details(value: object) -> dict[str, Any] | None:
    """Normalize parser-owned remediation facts for every public JSON surface."""
    if not isinstance(value, Mapping):
        return None

    def normalize(item: object) -> Any:
        if item is None or isinstance(item, (str, bool, int)):
            return item
        if isinstance(item, float):
            return item if math.isfinite(item) else str(item)
        if isinstance(item, (dt.date, dt.datetime)):
            return item.isoformat()
        if isinstance(item, Mapping):
            return {str(key): normalize(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(child) for child in item]
        return str(item)

    return {str(key): normalize(item) for key, item in value.items()}


def validate_arguments(action: object, arguments: Mapping[str, Any]) -> None:
    """Refuse arguments the selected action does not accept, exactly as a direct call does.

    `arguments` holds only what the caller sent; a field a caller omits counts as
    not supplied, the same as the `None` default the signature gives it.
    """
    values: dict[str, Any] = dict.fromkeys(operation_entries.RECORDS.every_field(), None)
    values.update(arguments)
    _validate_arguments(action, values)


def _validate_arguments(action: object, values: dict[str, Any]) -> None:
    if not isinstance(action, str) or action not in ACTIONS:
        _invalid_arguments()
    supplied = {name for name, value in values.items() if value is not None}
    missing = _REQUIRED_FIELDS[action] - supplied
    surplus = supplied - _ACTION_FIELDS[action]
    if missing or surplus:
        parts = []
        if surplus:
            parts.append(f"unexpected for {action}: " + ", ".join(sorted(surplus)))
        if missing:
            parts.append(f"missing for {action}: " + ", ".join(sorted(missing)))
        parts.append(
            action_argument_contract(action, _ACTION_FIELDS[action], _REQUIRED_FIELDS[action])
        )
        _invalid_arguments("; ".join(parts))
    if action == "history":
        _require_one_history_selector(values)
    if action == "append" and values["item"] is None and values["held"] is None:
        _invalid_arguments("append requires item or held")
    if (
        action == "update"
        and values["changes"] is None
        and values["refresh_presentation"] is not True
        and values["held"] is None
    ):
        _invalid_arguments("update requires changes, held, or refresh_presentation=true")
    if action in {"append", "update"} and values["hold"] is False and values["held"] is not None:
        # A resume that refuses again rewrites its held file in place, so
        # declining the hold would leave the previous attempt's diagnostics
        # standing as the answer to a question just asked again.
        _invalid_arguments("hold=false cannot be combined with held")
    if action == "update" and values["refresh_presentation"] not in {None, True}:
        _invalid_arguments("refresh_presentation must be true when supplied")
    if action == "validate" and ((values["collection"] is None) == (values["manifest_path"] is None)):
        _invalid_arguments("validate requires exactly one of: collection, manifest_path")
    if action == "validate" and values["collection"] is not None and values["scaffold"] is not None:
        _invalid_arguments("scaffold is not accepted when collection is supplied")
    if action == "query" and values["query"] is not None and supplied - {"collection", "query", "view"}:
        from .query_engine.validation import Finding

        legacy = sorted(supplied - {"collection", "query", "view"})
        repair = "Remove " + ", ".join(legacy) + ", or omit query."
        raise OpError(
            "QUERY_ARGUMENT_CONFLICT",
            "the query object replaces the legacy query arguments",
            repair,
            details=Finding("QUERY_ARGUMENT_CONFLICT", "query", "one query grammar", tuple(legacy), repair).as_dict(),
        )
    if action == "query" and values["view"] is not None and supplied & _QUERY_SHAPING_FIELDS:
        offending = sorted(supplied & _QUERY_SHAPING_FIELDS)
        _invalid_arguments("view excludes shaping fields: " + ", ".join(offending))
    if action == "query" and values["expand_children"] is True and values["expand_child"] is not None:
        _invalid_arguments("expand_children and expand_child are mutually exclusive")
    if action == "query" and values["item_key"] is not None and supplied - {"collection", "item_key"}:
        _invalid_arguments("a query with item_key reads that one row and its body, so it takes only collection; "
                           "remove " + ", ".join(sorted(supplied - {"collection", "item_key"})))


def _require_one_history_selector(given: Mapping[str, Any]) -> None:
    """Refuse a history call that selects its row by none of the selectors or by several."""
    chosen = [name for name in operation_entries.HISTORY_SELECTORS if given.get(name) is not None]
    if len(chosen) != 1:
        _invalid_arguments(
            "history takes exactly one of " + ", ".join(operation_entries.HISTORY_SELECTORS)
            + (f"; got {', '.join(chosen)}" if chosen else "")
        )


def refuse_arguments(detail: str) -> Never:
    """Raise this command's own argument refusal with `detail` as its explanation."""
    _invalid_arguments(detail)


def _invalid_arguments(detail: str | None = None) -> Never:
    message = "arguments do not match the selected record action"
    if detail:
        message = f"{message}: {detail}"
    raise OpError("INVALID_RECORD_ARGUMENTS", message)


def _is_direct_legacy_tracker_selector(collection: str) -> bool:
    """Only a literal Markdown path may opt into the legacy inspect route."""
    raw = collection.strip()
    return (
        raw.lower().endswith(".md")
        and Path(raw.replace("\\", "/")).name != "_collection.md"
        and not raw.lower().startswith("exomem://")
    )
