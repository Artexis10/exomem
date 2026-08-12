"""Single product-command dispatch for human-owned structured Records."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Never

from . import query_data, record_governance, records
from .cli_ops import OpError
from .structured_collections import CollectionError

ACTIONS = frozenset(
    {"describe", "validate", "inspect", "create", "query", "append", "update", "revise", "rebaseline"}
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
            "continuation",
            "include_agent_history",
            "output_format",
        }
    ),
    "append": frozenset(
        {"collection", "item", "why", "item_key", "expected_container_hash", "body"}
    ),
    "update": frozenset(
        {
            "collection",
            "item_key",
            "changes",
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
_REQUIRED_FIELDS = {
    "describe": frozenset(),
    "inspect": frozenset(),
    "validate": frozenset({"manifest_text"}),
    "create": frozenset({"manifest_path", "manifest_text", "why"}),
    "query": frozenset({"collection"}),
    "append": frozenset({"collection", "item", "why"}),
    "update": frozenset(
        {
            "collection",
            "item_key",
            "changes",
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
    }
)


def record_memory(
    vault_root: Path,
    action: Literal["describe", "validate", "inspect", "create", "query", "append", "update", "revise", "rebaseline"],
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
    continuation: str | None = None,
    include_agent_history: bool | None = None,
    output_format: Literal["json", "markdown", "csv"] | None = None,
    item: dict[str, Any] | None = None,
    item_key: str | None = None,
    expected_container_hash: str | None = None,
    expected_manifest_hash: str | None = None,
    acknowledged_gap_codes: list[str] | None = None,
    body: str | None = None,
    changes: dict[str, Any] | None = None,
    expected_item_version: str | None = None,
) -> dict[str, Any]:
    """Describe, validate, inspect, create, query, append, update, revise, or rebaseline Records.

    Records are human-owned event and state histories.  This command keeps the
    complete workflow on one product surface while routing mutations to guarded
    writers and reads through governance-aware projections.

    Args:
        action: describe, validate, inspect, create, query, append, or update.
        collection: Optional for inventory inspect; required for targeted reads/writes.
        manifest_path: Proposed manifest path for validate or create.
        manifest_text: Complete proposed manifest text for validate or create.
        why: Concise audit reason for create, append, or update.
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
        expand_children: Expand child values in query results.
        continuation: Snapshot-bound query continuation.
        include_agent_history: Include bounded governed agent mutation history.
        output_format: Query output format.
        item: Item values for append.
        item_key: Stable item ID for append or update.
        expected_container_hash: Exact current container hash for append or update.
        body: Optional Markdown item body for append.
        changes: Targeted changes for update.
        expected_item_version: Exact current item version for update.
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
        "continuation": continuation,
        "include_agent_history": include_agent_history,
        "output_format": output_format,
        "item": item,
        "item_key": item_key,
        "expected_container_hash": expected_container_hash,
        "expected_manifest_hash": expected_manifest_hash,
        "acknowledged_gap_codes": acknowledged_gap_codes,
        "body": body,
        "changes": changes,
        "expected_item_version": expected_item_version,
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
            assert item is not None
            assert why is not None
            manifest = record_governance.require_records_profile(
                record_governance.resolve_collection_for_mutation(vault_root, collection)
            )
            return records.append_record(
                vault_root,
                manifest,
                item=item,
                item_key=item_key,
                expected_container_hash=expected_container_hash,
                body="" if body is None else body,
                why=why,
            )
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
        assert changes is not None
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
            changes=changes,
            expected_container_hash=expected_container_hash,
            expected_item_version=expected_item_version,
            why=why,
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
    if not _REQUIRED_FIELDS[action] <= supplied or not supplied <= _ACTION_FIELDS[action]:
        _invalid_arguments()
    if action == "validate" and ((values["collection"] is None) == (values["manifest_path"] is None)):
        _invalid_arguments()
    if action == "validate" and values["collection"] is not None and values["scaffold"] is not None:
        _invalid_arguments()
    if action == "query" and values["view"] is not None and supplied & _QUERY_SHAPING_FIELDS:
        _invalid_arguments()


def _invalid_arguments() -> Never:
    raise OpError("INVALID_RECORD_ARGUMENTS", "arguments do not match the selected record action")


def _is_direct_legacy_tracker_selector(collection: str) -> bool:
    """Only a literal Markdown path may opt into the legacy inspect route."""
    raw = collection.strip()
    return (
        raw.lower().endswith(".md")
        and Path(raw.replace("\\", "/")).name != "_collection.md"
        and not raw.lower().startswith("exomem://")
    )
