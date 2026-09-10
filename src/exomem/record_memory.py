"""Single product-command dispatch for human-owned structured Records."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Never

from . import query_data, record_governance, records, structured_collections
from .cli_ops import OpError
from .structured_collections import CollectionError

ACTIONS = frozenset(
    {
        "describe",
        "validate",
        "inspect",
        "create",
        "query",
        "append",
        "update",
        "revise",
        "rebaseline",
        "discard",
    }
)

_ACTION_FIELDS = {
    "describe": frozenset(),
    "inspect": frozenset({"collection"}),
    "validate": frozenset({"collection", "manifest_path", "manifest_text", "scaffold"}),
    "create": frozenset({"manifest_path", "manifest_text", "why", "scaffold"}),
    "query": frozenset(
        {
            "collection",
            "view",
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
            "continuation",
            "include_agent_history",
            "output_format",
        }
    ),
    "append": frozenset(
        {
            "collection",
            "item",
            "why",
            "item_key",
            "expected_container_hash",
            "body",
            "delivery",
            "held",
            "hold",
        }
    ),
    "update": frozenset(
        {
            "collection",
            "item_key",
            "changes",
            "expected_container_hash",
            "expected_item_version",
            "why",
            "refresh_presentation",
            "held",
            "hold",
        }
    ),
    "discard": frozenset({"collection", "held", "why"}),
    "revise": frozenset(
        {"collection", "manifest_text", "expected_manifest_hash", "expected_container_hash", "why"}
    ),
    "rebaseline": frozenset(
        {"collection", "expected_manifest_hash", "expected_container_hash", "acknowledged_gap_codes", "why"}
    ),
}
_REQUIRED_FIELDS = {
    "describe": frozenset(),
    "inspect": frozenset(),
    "validate": frozenset({"manifest_text"}),
    "create": frozenset({"manifest_path", "manifest_text", "why"}),
    "query": frozenset({"collection"}),
    # `item` is required only when the caller is not resuming a held candidate,
    # which the argument rules below say in as many words.
    "append": frozenset({"collection", "why"}),
    "discard": frozenset({"collection", "held", "why"}),
    "update": frozenset(
        {
            "collection",
            "item_key",
            "expected_container_hash",
            "expected_item_version",
            "why",
        }
    ),
    "revise": frozenset(
        {"collection", "manifest_text", "expected_manifest_hash", "expected_container_hash", "why"}
    ),
    "rebaseline": frozenset(
        {"collection", "expected_manifest_hash", "expected_container_hash", "acknowledged_gap_codes", "why"}
    ),
}
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
        "update",
        "revise",
        "rebaseline",
        "discard",
    ],
    collection: str | None = None,
    manifest_path: str | None = None,
    manifest_text: str | None = None,
    why: str | None = None,
    scaffold: bool | None = None,
    view: str | None = None,
    filters: list[dict[str, Any]] | None = None,
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
) -> dict[str, Any]:
    """Describe, validate, inspect, create, query, append, update, revise, rebaseline, or discard Records.

    Records are human-owned event and state histories.  This command keeps the
    complete workflow on one product surface while routing mutations to guarded
    writers and reads through governance-aware projections.

    Args:
        action: describe, validate, inspect, create, query, append, update, revise,
            rebaseline, or discard.
        collection: Optional for inventory inspect; required for targeted reads/writes.
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
            key; supplying a natural-key value here refuses.
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
    """
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
    }
    _validate_arguments(action, values)
    try:
        if action == "describe":
            return parse_manifest_contract()
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
            return records.create_collection(
                vault_root,
                manifest_path,
                manifest_text,
                why=why,
                scaffold=True if scaffold is None else scaffold,
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
            return record_governance.project_query_result(
                result,
                manifest,
                output_format="json" if output_format is None else output_format,
                agent_history=history,
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
        raise OpError(
            error.code,
            error.reason,
            details=_json_safe_details(getattr(error, "details", None)),
        ) from error


def parse_manifest_contract() -> dict[str, Any]:
    """Project the parser-owned collection contract without vault content."""
    from .structured_collections import manifest_authoring_contract

    return manifest_authoring_contract()


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
        _invalid_arguments("; ".join(parts))
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
    if action == "query" and values["view"] is not None and supplied & _QUERY_SHAPING_FIELDS:
        offending = sorted(supplied & _QUERY_SHAPING_FIELDS)
        _invalid_arguments("view excludes shaping fields: " + ", ".join(offending))
    if action == "query" and values["expand_children"] is True and values["expand_child"] is not None:
        _invalid_arguments("expand_children and expand_child are mutually exclusive")


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
