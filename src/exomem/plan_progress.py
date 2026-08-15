"""Planned-versus-recorded review: authored intent next to recorded observation.

Planning already stores intent and may carry opaque `progress_evidence`
descriptors naming a Records collection, a role, and a saved view. Records
already stores observed state behind those views. Both profiles deliberately
stop before evaluation. This module is the read-only consumer that closes the
loop: it selects active committed Planning items that carry evidence, runs each
bound saved view through the governed Records read path, and presents the two
sides together.

It measures; it does not reason. There is no `health` verdict, no ratio, no
percentage, no ranking, and no ordering by severity. Divergence is exact
integers, and every judgment is left to the reader. Nothing here reaches a
mutation path, so the vault is byte-identical after a review.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import planning, record_governance
from . import structured_collections as collections
from .structured_collections import CollectionError

MODE = "plan-progress"
DEFAULT_ITEM_LIMIT = 25
MAX_ITEM_LIMIT = 100
DEFAULT_EXECUTION_BUDGET = 64
MAX_EXECUTION_BUDGET = 256
#: Planning already bounds authored evidence at 16; re-stated so the reader is
#: bounded even against a directly edited item.
MAX_EVIDENCE = 16
ROLES = ("progress", "completion")
UNAVAILABLE_REASONS = (
    "collection_unavailable",
    "profile_mismatch",
    "view_unavailable",
    "query_unavailable",
    "result_withheld",
    "budget_exhausted",
)

_DIVERGENCE_KEYS = (
    "evidence_bindings",
    "resolved_bindings",
    "unresolved_bindings",
    "progress_bindings",
    "completion_bindings",
    "progress_observations",
    "completion_observations",
)
_INTENT_FIELDS = (
    "title",
    "kind",
    "status",
    "lifecycle",
    "priority",
    "commitment",
    "horizon",
    "health",
    "window_start",
    "window_end",
)
_EVIDENCE_FIELD = "progress_evidence"
_VIEW_ERROR_CODES = frozenset(
    {
        "SAVED_VIEW_NOT_FOUND",
        "SAVED_VIEW_NOT_AVAILABLE",
        "INVALID_SAVED_VIEW",
        "STALE_SAVED_VIEW",
    }
)


def selects_item(row: Any) -> bool:
    """Return whether one Planning row is inside the reviewed slice.

    The slice is deliberately narrow: an active-lifecycle item whose authored
    status is `active`, whose commitment is `committed`, and which actually
    names evidence. Anything else has nothing to compare yet.
    """
    if not isinstance(row, Mapping):
        return False
    return (
        row.get("lifecycle") == "active"
        and row.get("status") == "active"
        and row.get("commitment") == "committed"
        and bool(evidence_bindings(row))
    )


def evidence_bindings(row: Any) -> list[dict[str, str]]:
    """Return the authored evidence descriptors, in order, skipping invalid ones."""
    if not isinstance(row, Mapping):
        return []
    authored = row.get(_EVIDENCE_FIELD)
    if not isinstance(authored, list):
        return []
    bindings: list[dict[str, str]] = []
    for descriptor in authored[:MAX_EVIDENCE]:
        if not isinstance(descriptor, Mapping) or set(descriptor) != {
            "collection",
            "role",
            "view",
        }:
            continue
        reference = descriptor["collection"]
        role = descriptor["role"]
        view = descriptor["view"]
        if type(reference) is not str or not reference:
            continue
        if role not in ROLES:
            continue
        if type(view) is not str or not view:
            continue
        bindings.append({"collection": reference, "role": role, "view": view})
    return bindings


def divergence(entries: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """Return the exact count block for one item's assembled evidence.

    Every value is a non-negative integer. No ratio, percentage, estimate, or
    verdict is derived from them here or anywhere downstream.
    """
    counts = dict.fromkeys(_DIVERGENCE_KEYS, 0)
    for entry in entries:
        counts["evidence_bindings"] += 1
        role = entry.get("role")
        if role in ROLES:
            counts[f"{role}_bindings"] += 1
        observed = entry.get("observed")
        if entry.get("resolved") is True and isinstance(observed, Mapping):
            counts["resolved_bindings"] += 1
            matched = observed.get("matched")
            if role in ROLES and type(matched) is int and matched >= 0:
                counts[f"{role}_observations"] += matched
        else:
            counts["unresolved_bindings"] += 1
    return counts


def order_items(items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Order by identity alone, so position never implies priority."""
    return sorted(
        items,
        key=lambda item: (str(item.get("collection_id", "")), str(item.get("plan_id", ""))),
    )


def review(
    vault_root: Path,
    *,
    collection: str | None = None,
    limit: int = DEFAULT_ITEM_LIMIT,
    execution_budget: int = DEFAULT_EXECUTION_BUDGET,
) -> dict[str, Any]:
    """Present authored intent next to the observations its evidence binds."""
    root = Path(vault_root)
    item_limit = _bounded(limit, DEFAULT_ITEM_LIMIT, MAX_ITEM_LIMIT)
    budget = _bounded(execution_budget, DEFAULT_EXECUTION_BUDGET, MAX_EXECUTION_BUDGET)
    manifests, unresolved_selector = _planning_manifests(root, collection)

    scanned = 0
    collections_unavailable = unresolved_selector
    query_truncated = False
    matched_rows: list[Mapping[str, Any]] = []
    for manifest in manifests:
        payload = _planning_page(root, manifest)
        if payload is None:
            collections_unavailable += 1
            continue
        scanned += 1
        query_truncated = query_truncated or payload.get("truncated") is True
        matched_rows.extend(row for row in payload.get("rows", []) if selects_item(row))

    # One ordering implementation, used here: identity order decides which items
    # truncation retains, so a ranking introduced anywhere would change both the
    # sequence and the retained set.
    selected = order_items(matched_rows)
    items_matched = len(selected)
    retained = selected[:item_limit]

    unavailable = dict.fromkeys(UNAVAILABLE_REASONS, 0)
    executions: dict[tuple[str, str], dict[str, Any] | str] = {}
    executed = 0
    bindings_truncated = False
    items: list[dict[str, Any]] = []
    for row in retained:
        entries: list[dict[str, Any]] = []
        for binding in evidence_bindings(row):
            key = (binding["collection"], binding["view"])
            outcome = executions.get(key)
            if outcome is None:
                if executed >= budget:
                    outcome = "budget_exhausted"
                    bindings_truncated = True
                else:
                    executed += 1
                    outcome = _observe(root, binding["collection"], binding["view"])
                    executions[key] = outcome
            resolved = isinstance(outcome, Mapping)
            if not resolved:
                unavailable[str(outcome)] += 1
            entries.append(
                {
                    "role": binding["role"],
                    "view": binding["view"],
                    "collection": binding["collection"],
                    "resolved": resolved,
                    "unresolved_reason": None if resolved else str(outcome),
                    "observed": dict(outcome) if resolved else None,
                }
            )
        items.append(
            {
                "plan_ref": collections.plan_ref(row["collection_id"], row["plan_id"]),
                "collection_id": row["collection_id"],
                "plan_id": row["plan_id"],
                "intent": {name: row.get(name) for name in _INTENT_FIELDS},
                "evidence": entries,
                "divergence": divergence(entries),
            }
        )

    return {
        "mode": MODE,
        "derived": True,
        "read_only": True,
        "generated_at": dt.datetime.now(tz=dt.UTC).isoformat().replace("+00:00", "Z"),
        "collections_scanned": scanned,
        "collections_unavailable": collections_unavailable,
        "items_matched": items_matched,
        "items_reviewed": len(items),
        "truncated": items_matched > item_limit or query_truncated,
        "bindings_executed": executed,
        "bindings_truncated": bindings_truncated,
        "unavailable": unavailable,
        "items": items,
    }


def _planning_manifests(
    root: Path, selector: str | None
) -> tuple[list[collections.CollectionManifest], int]:
    """Resolve the Planning collections to scan, authorizing before resolving."""
    if selector is not None:
        try:
            manifest = record_governance.resolve_collection(root, selector)
        except CollectionError:
            return [], 1
        if manifest.semantic_profile != "planning":
            return [], 1
        return [manifest], 0
    try:
        discovered = collections.discover_collections(
            root, authorize_path=record_governance.full_release_filter(root)
        )
    except CollectionError:
        return [], 0
    return [
        manifest for manifest in discovered if manifest.semantic_profile == "planning"
    ], 0


def _planning_page(
    root: Path, manifest: collections.CollectionManifest
) -> Mapping[str, Any] | None:
    """Run one bounded Planning query, projecting away bodies we never present."""
    columns = [
        name
        for name in (*_INTENT_FIELDS, _EVIDENCE_FIELD)
        if name in manifest.schema.fields
    ]
    try:
        return planning.query(
            root,
            manifest.path,
            filters=[
                {"column": "status", "op": "eq", "value": "active"},
                {"column": "commitment", "op": "eq", "value": "committed"},
            ],
            columns=columns,
            sort_by="title",
            limit=MAX_ITEM_LIMIT,
            lifecycle="active",
        )
    except CollectionError:
        return None


def _observe(root: Path, reference: str, view: str) -> dict[str, Any] | str:
    """Execute one bound Records saved view, or name why it could not run.

    Authorization precedes resolution and resolution precedes any canonical
    parse, exactly as the Records read path already requires. A missing target
    and a withheld target return the same reason so the review cannot be used
    to probe for hidden collections.
    """
    try:
        manifest = record_governance.resolve_collection(root, reference)
    except CollectionError:
        return "collection_unavailable"
    if manifest.semantic_profile != "records":
        return "profile_mismatch"
    try:
        result = record_governance.query_collection(root, manifest, view=view)
    except CollectionError as error:
        if error.code in _VIEW_ERROR_CODES:
            return "view_unavailable"
        if error.code == "COLLECTION_NOT_FOUND":
            return "collection_unavailable"
        if error.code == "RECORDS_PROFILE_REQUIRED":
            return "profile_mismatch"
        return "query_unavailable"
    projected = record_governance.project_query_result(result, manifest)
    matched = projected.get("total_matched")
    returned = projected.get("returned")
    truncated = projected.get("truncated")
    if (
        projected.get("withheld")
        or type(matched) is not int
        or type(returned) is not int
        or type(truncated) is not bool
    ):
        return "result_withheld"
    # The view's declared aggregate is deliberately NOT passed through. A saved
    # view may declare `latest:<col>` (a whole record row, including record_id
    # and item_version), `distinct:<col>` / `group:<col>` (record values), or
    # `avg:<col>` (a float mean) — rows, identities, and a score-shaped value,
    # all three of which this review refuses to emit. `total_matched` is
    # computed identically under every aggregate shape, so dropping the
    # aggregate costs the reader nothing.
    return {
        "collection_id": projected.get("collection_id"),
        "snapshot": projected.get("snapshot"),
        "matched": matched,
        "returned": returned,
        "truncated": truncated,
    }


def _bounded(value: Any, default: int, maximum: int) -> int:
    if type(value) is not int or value < 1:
        return default
    return min(value, maximum)
