"""Single product-command dispatch for human-owned Planning collections."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Never

from . import planning
from .cli_ops import OpError
from .structured_collections import CollectionError

ACTIONS = frozenset(
    {
        "inspect",
        "validate",
        "create",
        "query",
        "add",
        "update",
        "triage",
        "revise",
        "rebaseline",
    }
)
_ACTION_FIELDS = {
    "inspect": frozenset({"collection"}),
    "validate": frozenset({"collection", "manifest_path", "manifest_text"}),
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
            "lifecycle",
            "hierarchy_mode",
            "hierarchy_depth",
            "hierarchy_limit",
            "continuation",
            "include_agent_history",
            "output_format",
        }
    ),
    "add": frozenset({"collection", "item", "plan_id", "expected_container_hash", "body", "why"}),
    "update": frozenset(
        {
            "collection",
            "plan_id",
            "expected_container_hash",
            "expected_item_version",
            "why",
            "changes",
            "body",
        }
    ),
    "triage": frozenset(
        {
            "collection",
            "plan_id",
            "expected_container_hash",
            "expected_item_version",
            "why",
            "transition",
        }
    ),
    "revise": frozenset(
        {
            "collection",
            "manifest_text",
            "expected_manifest_hash",
            "expected_container_hash",
            "why",
        }
    ),
    "rebaseline": frozenset(
        {
            "collection",
            "expected_manifest_hash",
            "expected_container_hash",
            "acknowledged_gap_codes",
            "why",
        }
    ),
}
_REQUIRED_FIELDS = {
    "inspect": frozenset({"collection"}),
    "validate": frozenset({"manifest_text"}),
    "create": frozenset({"manifest_path", "manifest_text", "why"}),
    "query": frozenset({"collection"}),
    "add": frozenset({"collection", "item", "why"}),
    "update": frozenset(
        {"collection", "plan_id", "expected_container_hash", "expected_item_version", "why"}
    ),
    "triage": frozenset(
        {
            "collection",
            "plan_id",
            "expected_container_hash",
            "expected_item_version",
            "why",
            "transition",
        }
    ),
    "revise": frozenset(
        {
            "collection",
            "manifest_text",
            "expected_manifest_hash",
            "expected_container_hash",
            "why",
        }
    ),
    "rebaseline": frozenset(
        {
            "collection",
            "expected_manifest_hash",
            "expected_container_hash",
            "acknowledged_gap_codes",
            "why",
        }
    ),
}
_VIEW_SHAPING_FIELDS = frozenset(
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
        "lifecycle",
    }
)


def plan_memory(
    vault_root: Path,
    action: Literal[
        "inspect",
        "validate",
        "create",
        "query",
        "add",
        "update",
        "triage",
        "revise",
        "rebaseline",
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
    lifecycle: Literal["active", "archived", "all"] | None = None,
    hierarchy_mode: Literal["none", "ancestors", "descendants"] | None = None,
    hierarchy_depth: int | None = None,
    hierarchy_limit: int | None = None,
    continuation: str | None = None,
    include_agent_history: bool | None = None,
    output_format: Literal["json", "markdown", "csv"] | None = None,
    item: dict[str, Any] | None = None,
    plan_id: str | None = None,
    expected_manifest_hash: str | None = None,
    expected_container_hash: str | None = None,
    acknowledged_gap_codes: list[str] | None = None,
    body: str | None = None,
    changes: dict[str, Any] | None = None,
    transition: dict[str, Any] | None = None,
    expected_item_version: str | None = None,
) -> dict[str, Any]:
    """Work with human-owned intended future state through one Planning surface.

    `inspect`, `validate`, and `query` are read-only. `create`, `add`, `update`,
    `triage`, `revise`, and `rebaseline` are guarded mutations. Planning stores
    goals, outcomes, initiatives, work items, horizons, priorities, and explicit
    commitments; observed events belong in `record_memory`, while accepted
    software change contracts and execution truth remain in the repository.

    A plan's UUID is durable identity, not its reader-facing filename. New
    collections can declare human filenames and managed presentation blocks;
    existing UUID collections move only through an explicit read-only
    `maintain_memory(mode="structured-files")` preview followed by exact-plan
    apply. Never infer completion or horizon changes from elapsed time.
    """
    values = locals().copy()
    values.pop("vault_root")
    values.pop("action")
    _validate_arguments(action, values)
    try:
        if action == "inspect":
            assert collection is not None
            return planning.inspect(vault_root, collection)
        if action == "validate":
            assert manifest_text is not None
            return planning.validate(
                vault_root,
                mode="revision" if collection is not None else "create",
                manifest_text=manifest_text,
                manifest_path=manifest_path,
                collection=collection,
            )
        if action == "create":
            assert manifest_path is not None and manifest_text is not None and why is not None
            return planning.create_collection(
                vault_root,
                manifest_path,
                manifest_text,
                why=why,
                scaffold=True if scaffold is None else scaffold,
            )
        if action == "add":
            assert collection is not None and item is not None and why is not None
            return planning.add(
                vault_root,
                collection,
                item=item,
                plan_id=plan_id,
                expected_container_hash=expected_container_hash,
                body="" if body is None else body,
                why=why,
            )
        if action == "query":
            assert collection is not None
            return planning.query(
                vault_root,
                collection,
                view=view,
                filters=filters,
                columns=columns,
                sort_by=sort_by,
                descending=False if descending is None else descending,
                limit=100 if limit is None else limit,
                aggregate=aggregate,
                date_from=date_from,
                date_to=date_to,
                date_column=date_column,
                continuation=continuation,
                output_format="json" if output_format is None else output_format,
                hierarchy_mode="none" if hierarchy_mode is None else hierarchy_mode,
                hierarchy_depth=3 if hierarchy_depth is None else hierarchy_depth,
                hierarchy_limit=100 if hierarchy_limit is None else hierarchy_limit,
                lifecycle="active" if lifecycle is None else lifecycle,
                include_agent_history=(
                    False if include_agent_history is None else include_agent_history
                ),
            )
        if action == "update":
            assert collection is not None and plan_id is not None
            assert (
                expected_container_hash is not None
                and expected_item_version is not None
                and why is not None
            )
            return planning.update(
                vault_root,
                collection,
                plan_id=plan_id,
                changes={} if changes is None else changes,
                body=body,
                expected_container_hash=expected_container_hash,
                expected_item_version=expected_item_version,
                why=why,
            )
        if action == "triage":
            assert collection is not None and plan_id is not None and transition is not None
            assert (
                expected_container_hash is not None
                and expected_item_version is not None
                and why is not None
            )
            return planning.triage(
                vault_root,
                collection,
                plan_id=plan_id,
                transition=transition,
                expected_container_hash=expected_container_hash,
                expected_item_version=expected_item_version,
                why=why,
            )
        if action == "revise":
            assert collection is not None and manifest_text is not None
            assert expected_manifest_hash is not None and expected_container_hash is not None
            assert why is not None
            return planning.revise(
                vault_root,
                collection,
                manifest_text=manifest_text,
                expected_manifest_hash=expected_manifest_hash,
                expected_container_hash=expected_container_hash,
                why=why,
            )
        if action == "rebaseline":
            assert collection is not None and acknowledged_gap_codes is not None
            assert expected_manifest_hash is not None and expected_container_hash is not None
            assert why is not None
            return planning.rebaseline(
                vault_root,
                collection,
                expected_manifest_hash=expected_manifest_hash,
                expected_container_hash=expected_container_hash,
                acknowledged_gap_codes=acknowledged_gap_codes,
                why=why,
            )
        raise CollectionError("INVALID_PLAN_ARGUMENTS", "Planning action is not available")
    except CollectionError as error:
        code = _public_error_code(error)
        raise OpError(code, _public_error_message(code, error.reason)) from error


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
    if action == "query" and values["view"] is not None and supplied & _VIEW_SHAPING_FIELDS:
        offending = sorted(supplied & _VIEW_SHAPING_FIELDS)
        _invalid_arguments("view excludes shaping fields: " + ", ".join(offending))
    if action == "validate" and (
        (values["collection"] is None) == (values["manifest_path"] is None)
    ):
        _invalid_arguments("validate requires exactly one of: collection, manifest_path")
    if action == "update" and values["changes"] is None and values["body"] is None:
        _invalid_arguments("update requires at least one of: changes, body")


def _invalid_arguments(detail: str | None = None) -> Never:
    message = "arguments do not match the selected Planning action"
    if detail:
        message = f"{message}: {detail}"
    raise OpError("INVALID_PLAN_ARGUMENTS", message)


def _public_error_code(error: CollectionError) -> str:
    if error.code == "RECORD_NOT_FOUND":
        return "PLAN_NOT_FOUND"
    if error.code == "AMBIGUOUS_RECORD":
        return "AMBIGUOUS_PLAN"
    if error.code == "RECORD_ID_CONFLICT":
        return "PLAN_ID_CONFLICT"
    if error.code == "STALE_RECORD":
        return "STALE_PLAN_ITEM" if "item" in error.reason else "STALE_PLAN_CONTAINER"
    if error.code == "INVALID_RECORD_CONTINUATION":
        return "INVALID_PLAN_CONTINUATION"
    if error.code == "STALE_RECORD_SNAPSHOT":
        return "STALE_PLAN_SNAPSHOT"
    if error.code == "RECORD_RESPONSE_TOO_LARGE":
        return "PLAN_RESPONSE_TOO_LARGE"
    return error.code


def _public_error_message(code: str, reason: str) -> str:
    if code == "INVALID_PLAN_RELATION":
        return "Planning relationship is not available"
    return reason[:512] or "Planning request could not be completed"
