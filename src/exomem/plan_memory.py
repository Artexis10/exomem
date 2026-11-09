"""Single product-command dispatch for human-owned Planning collections."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Never

from . import collection_refs, operation_entries, planning
from .cli_ops import OpError, action_argument_contract
from .query_compat import public_filter_annotation
from .structured_collections import CollectionError, collection_remediation

_QueryFilters = public_filter_annotation()

#: The Planning spec caps every Planning refusal message at 512 UTF-8 bytes.
_REFUSAL_MESSAGE_MAX_BYTES = 512

# The operation entries are the one table of what each action accepts and requires.
ACTIONS = operation_entries.PLANS.actions()
_ACTION_FIELDS = operation_entries.PLANS.action_fields()
_REQUIRED_FIELDS = operation_entries.PLANS.required_fields()
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
    item_id: str | None = None,
) -> dict[str, Any]:
    """Manage intent; record_memory holds outcomes.

    Inspect/query/validate/history read; others write. Writes select one queried item.
    Reread if stale; time never proves completion.
    Help: bootstrap(section="records_planning").

    Args:
        collection: Vault-relative manifest path, collection UUID or memory reference. Omit to list.
        plan_id: row plan_id; on query, that item and its body.
        expected_container_hash: Returned snapshot.
        expected_item_version: row item_version.
        changes: Update.
        transition: Triage.
        item_id: update and triage: the item `id` a query returned. It stands for plan_id,
            expected_item_version and expected_container_hash, which the call reads under
            the writer lease. A stale id is refused with the current one.
    """
    collection = collection_refs.resolve_collection(vault_root, collection, profile="planning")
    guards = _bound_item(
        vault_root, action, collection, item_id, (plan_id, expected_container_hash, expected_item_version)
    )
    values = locals().copy()
    for name in ("vault_root", "action", "item_id", "guards"):
        values.pop(name)
    values.update(guards)
    plan_id = values["plan_id"]
    _validate_arguments(action, values)
    if action in {"update", "triage"} and None in (
        values["plan_id"], values["expected_container_hash"], values["expected_item_version"]
    ):
        _invalid_arguments("update and triage need item_id, or plan_id with expected_container_hash and expected_item_version")
    try:
        # nosemgrep: ep-word-membership -- plan_memory's closed write actions (see ACTIONS).
        if action in {"add", "update", "triage"}:
            return _write(vault_root, action, values)
        if action == "inspect" and collection is None:
            from .record_governance import inventory_collections

            return inventory_collections(vault_root, semantic_profile="planning")
        from .collection_store import preview

        selected, result = preview.dispatch(vault_root, "planning", action, values)
        if selected:
            return collection_refs.mark_rows(result, profile="planning") if action == "query" else result
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
        if action == "history":
            assert collection is not None and plan_id is not None
            from . import record_history

            return record_history.read(
                vault_root,
                collection,
                item_key=plan_id,
                semantic_profile="planning",
                limit=limit,
                continuation=continuation,
            )
        if action == "query" and plan_id is not None:
            from . import record_governance

            # Resolved under the caller's authority first, so the refusal never confirms a withheld collection.
            assert collection is not None
            planning.require_planning_profile(record_governance.resolve_collection(vault_root, collection))
            raise CollectionError(
                "QUERY_UNAVAILABLE", "a query with plan_id reads a store-routed collection; this one keeps "
                "each body in its item file, so read that file with read_memory"
            )
        if action == "query":
            assert collection is not None
            return collection_refs.mark_rows(planning.query(
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
            ), profile="planning")
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
        # The parser knows which manifest the caller should have named; this
        # surface drops the rest of `details` but must not drop that.
        raise OpError(
            code,
            _public_error_message(code, error.reason),
            collection_remediation(error),
        ) from error


def _bound_item(
    vault_root: Path, action: str, collection: str | None, item_id: str | None,
    given: tuple[str | None, ...],
) -> dict[str, str]:
    """The plan id and guards that `item_id` stands for, read through this command's own query."""
    if item_id is None:
        return {}
    if action not in {"update", "triage"} or any(value is not None for value in given):
        _invalid_arguments("item_id applies to update and triage and replaces plan_id and both guards")
    bound = collection_refs.bind_item(
        lambda **kw: plan_memory(vault_root, "query", lifecycle="all", **kw),
        collection=str(collection), item_id=item_id, profile="planning",
    )
    return {
        "plan_id": bound["item_key"], "expected_item_version": bound["expected_item_version"],
        "expected_container_hash": bound["expected_container_hash"],
    }


def validate_arguments(action: object, arguments: Mapping[str, Any]) -> None:
    """Refuse arguments the selected action does not accept, exactly as a direct call does.

    `arguments` holds only what the caller sent; a field a caller omits counts as
    not supplied, the same as the `None` default the signature gives it.
    """
    values: dict[str, Any] = dict.fromkeys(operation_entries.PLANS.every_field(), None)
    values.update(arguments)
    _validate_arguments(action, values)


def _write(vault_root: Path, action: str, values: Mapping[str, Any], *, _prepare=None, request_id=None, history_binding=None, _validate_inputs=None) -> dict[str, Any]:
    """The shared native Planning write facade; private preparation stays off the public schema."""
    from .collection_store import preview

    args = {key: value for key, value in values.items() if value is not None}
    _validate_arguments(action, {**dict.fromkeys(set().union(*_ACTION_FIELDS.values())), **args})
    internal = {"_prepare": _prepare, "hold": False} if _prepare is not None else {}
    if _validate_inputs is not None:
        internal["_validate_inputs"] = _validate_inputs
    if request_id is not None:
        internal["request_id"] = request_id
    selected, result = preview.dispatch(vault_root, "planning", action, {**args, **internal})
    if selected:
        return result
    args.pop("request_id", None)
    if action == "add":
        return planning.add(vault_root, **args, _prepare=_prepare, _validate_inputs=_validate_inputs)
    if action == "update":
        args.setdefault("changes", {})
        return planning.update(vault_root, **args, _prepare=_prepare, _history_binding=history_binding, _validate_inputs=_validate_inputs)
    if action == "triage":
        return planning.triage(vault_root, **args, _prepare=_prepare, _history_binding=history_binding, _validate_inputs=_validate_inputs)
    raise ValueError("native Planning write action is invalid")


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
        _invalid_arguments(
            "; ".join(parts),
            accepted=action_argument_contract(
                action, _ACTION_FIELDS[action], _REQUIRED_FIELDS[action]
            ),
        )
    if action == "query" and values["view"] is not None and supplied & _VIEW_SHAPING_FIELDS:
        offending = sorted(supplied & _VIEW_SHAPING_FIELDS)
        _invalid_arguments("view excludes shaping fields: " + ", ".join(offending))
    if action == "validate" and (
        (values["collection"] is None) == (values["manifest_path"] is None)
    ):
        _invalid_arguments("validate requires exactly one of: collection, manifest_path")
    if action == "update" and values["changes"] is None and values["body"] is None:
        _invalid_arguments("update requires at least one of: changes, body")
    if action == "query" and values["plan_id"] is not None and supplied - {"collection", "plan_id"}:
        _invalid_arguments("a query with plan_id reads that one item and its body, so it takes only collection; "
                           "remove " + ", ".join(sorted(supplied - {"collection", "plan_id"})))


def refuse_arguments(detail: str) -> Never:
    """Raise this command's own argument refusal with `detail` as its explanation."""
    _invalid_arguments(detail)


def _invalid_arguments(detail: str | None = None, *, accepted: str | None = None) -> Never:
    message = "arguments do not match the selected Planning action"
    if detail:
        message = f"{message}: {detail}"
    # The accepted-argument list is help, not the refusal itself: drop it
    # rather than exceed the Planning message cap.
    if accepted and len(f"{message}; {accepted}".encode()) <= _REFUSAL_MESSAGE_MAX_BYTES:
        message = f"{message}; {accepted}"
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
