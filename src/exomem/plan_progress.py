"""Planned-versus-recorded review: authored intent next to recorded observation.

Planning already stores intent and may carry opaque `progress_evidence`
descriptors naming a Records collection, a role, and a saved view. Records
already stores observed state behind those views. Both profiles deliberately
stop before evaluation. This module is the read-only consumer that closes the
loop: it selects active committed Planning items that carry evidence,
motivating knowledge, or both, runs each bound saved view through the governed
Records read path, and presents the two sides together.

A Planning item may also carry `motivation`: a bounded list of
`exomem://memory/` references to the knowledge that motivates it. The same
reader resolves those references and reports which of them the vault has since
superseded, so a plan premised on a replaced belief stops executing
unexamined. That is the whole claim — the successor is never named, and no
verdict is derived from the supersession.

It measures; it does not reason. There is no `health` verdict, no ratio, no
percentage, no ranking, and no ordering by severity. Divergence is exact
integers, and every judgment is left to the reader. Nothing here reaches a
mutation path, so the vault is byte-identical after a review — including the
reference sidecar, which a canonical byte census would not notice.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import find_corpus, memory_refs, planning, record_governance
from . import structured_collections as collections
from .structured_collections import CollectionError

MODE = "plan-progress"
DEFAULT_ITEM_LIMIT = 25
MAX_ITEM_LIMIT = 100
DEFAULT_EXECUTION_BUDGET = 64
MAX_EXECUTION_BUDGET = 256
#: Deliberately a second budget rather than a share of `execution_budget`, so
#: `budget_exhausted` keeps meaning exactly one thing: a Records view was
#: skipped.
DEFAULT_MOTIVATION_BUDGET = 64
MAX_MOTIVATION_BUDGET = 256
#: Planning already bounds authored evidence at 16; re-stated so the reader is
#: bounded even against a directly edited item.
MAX_EVIDENCE = 16
#: Planning bounds `motivation` at 16 the same way, and for the same reason.
MAX_MOTIVATION = 16
#: A well-formed `exomem://memory/<uuid>` is 52 characters. The cap only stops
#: a directly-edited item from making the reader echo an unbounded string.
MAX_MOTIVATION_REFERENCE = 128
ROLES = ("progress", "completion")
UNAVAILABLE_REASONS = (
    "collection_unavailable",
    "profile_mismatch",
    "view_unavailable",
    "query_unavailable",
    "result_withheld",
    "budget_exhausted",
    "motivation_unavailable",
    "motivation_budget_exhausted",
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
#: Counts, never flags: `type(True) is int` is False, and the whole divergence
#: block is asserted to be plain integers.
_MOTIVATION_DIVERGENCE_KEYS = (
    "motivation_refs",
    "motivation_resolved",
    "motivation_unresolved",
    "motivation_superseded",
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
_MOTIVATION_FIELD = "motivation"
#: The one outcome every motivation refusal collapses into. A reference the
#: vault does not hold, one it holds twice, one that is malformed, one whose
#: page is blocked, and one whose page this reader is not released are the same
#: string, so the review cannot be used to probe for hidden knowledge.
#: The malformed case is unreachable through a governed collection — `query`
#: normalizes stored records and refuses the whole collection first, which the
#: review reports through the equally bounded `collections_unavailable`. It is
#: handled here anyway, because that is the behaviour if the validation ever
#: loosens, and it is covered at the unit level rather than end to end.
_MOTIVATION_UNAVAILABLE = "motivation_unavailable"
_MOTIVATION_BUDGET_EXHAUSTED = "motivation_budget_exhausted"
_SUPERSEDED_STATUS = "superseded"
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
    names something to examine — evidence bindings, motivating knowledge, or
    both. Anything else has nothing to compare yet.

    Requiring evidence alone would leave the supersession review silent on
    every real case, because a plan may cite the belief that motivates it
    without ever binding a Records view to it.
    """
    if not isinstance(row, Mapping):
        return False
    return (
        row.get("lifecycle") == "active"
        and row.get("status") == "active"
        and row.get("commitment") == "committed"
        and bool(evidence_bindings(row) or motivation_refs(row))
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


def motivation_refs(row: Any) -> list[str]:
    """Return the authored motivation references, in order, bounded.

    A syntactically malformed reference is deliberately kept. An invalid
    reference is one of the outcomes the single unavailable reason folds
    together, and silently dropping it here would make it the one case a
    caller could tell apart — by counting the entries that came back. Only a
    value that is not a bounded non-empty string is dropped, exactly as an
    unusable evidence descriptor is dropped.
    """
    if not isinstance(row, Mapping):
        return []
    authored = row.get(_MOTIVATION_FIELD)
    if not isinstance(authored, list):
        return []
    refs: list[str] = []
    for reference in authored[:MAX_MOTIVATION]:
        if type(reference) is not str or not reference:
            continue
        if len(reference) > MAX_MOTIVATION_REFERENCE:
            continue
        refs.append(reference)
    return refs


def divergence(
    entries: Sequence[Mapping[str, Any]],
    motivation: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, int]:
    """Return the exact count block for one item's assembled evidence.

    Every value is a non-negative integer. No ratio, percentage, estimate, or
    verdict is derived from them here or anywhere downstream. In particular no
    value is a boolean: a plan premised on superseded knowledge is reported as
    a count of superseded references, not as a flag, so the block stays one
    kind of thing.

    `motivation` is optional and defaults to absent rather than empty. A caller
    that supplies it — including as an empty sequence — gets the four
    motivation counts as well, so every reviewed item carries them. A caller
    that omits it gets exactly the seven evidence counts, which is the shape
    this function has always returned to a single-argument call.
    """
    counts = dict.fromkeys(_DIVERGENCE_KEYS, 0)
    if motivation is not None:
        counts.update(dict.fromkeys(_MOTIVATION_DIVERGENCE_KEYS, 0))
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
    for entry in motivation or ():
        counts["motivation_refs"] += 1
        if entry.get("resolved") is True:
            counts["motivation_resolved"] += 1
            if entry.get("superseded") is True:
                counts["motivation_superseded"] += 1
        else:
            counts["motivation_unresolved"] += 1
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
    motivation_budget: int = DEFAULT_MOTIVATION_BUDGET,
) -> dict[str, Any]:
    """Present authored intent next to what its evidence and motivation name.

    Two budgets, deliberately separate: `execution_budget` caps distinct
    Records saved-view executions, `motivation_budget` caps distinct memory
    identities resolved. Neither verdict depends on what a target turns out to
    be.
    """
    root = Path(vault_root)
    item_limit = _bounded(limit, DEFAULT_ITEM_LIMIT, MAX_ITEM_LIMIT)
    budget = _bounded(execution_budget, DEFAULT_EXECUTION_BUDGET, MAX_EXECUTION_BUDGET)
    motivation_cap = _bounded(
        motivation_budget, DEFAULT_MOTIVATION_BUDGET, MAX_MOTIVATION_BUDGET
    )
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

    # Motivation targets are collected across the RETAINED items, after
    # ordering and truncation: collected earlier, the budget would be spent on
    # items that never appear. The budget verdict then comes from a counter
    # over that list, before any target is consulted — which is what keeps
    # `motivation_budget_exhausted` existence-independent, and therefore not a
    # second probe channel beside the collapsed unavailable reason.
    distinct_ids: list[str] = []
    for row in retained:
        for reference in motivation_refs(row):
            identifier = memory_refs.parse_memory_ref(reference)
            if identifier is not None and identifier not in distinct_ids:
                distinct_ids.append(identifier)
    motivation_consulted = min(len(distinct_ids), motivation_cap)
    motivation_truncated = len(distinct_ids) > motivation_cap
    consulted = frozenset(distinct_ids[:motivation_consulted])
    supersession = _supersession(root, distinct_ids[:motivation_consulted])

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
        motivation: list[dict[str, Any]] = []
        for reference in motivation_refs(row):
            identifier = memory_refs.parse_memory_ref(reference)
            if identifier is not None and identifier not in consulted:
                reason: str | None = _MOTIVATION_BUDGET_EXHAUSTED
            elif identifier is None or identifier not in supersession:
                reason = _MOTIVATION_UNAVAILABLE
            else:
                reason = None
            if reason is not None:
                unavailable[reason] += 1
            motivation.append(
                {
                    # Never `ref`: a plan-progress response carries no
                    # triageable reference, and the authored value is echoed
                    # back rather than newly minted.
                    "memory": reference,
                    "resolved": reason is None,
                    "unresolved_reason": reason,
                    "superseded": (
                        supersession[identifier]
                        if reason is None and identifier is not None
                        else None
                    ),
                }
            )
        items.append(
            {
                "plan_ref": collections.plan_ref(row["collection_id"], row["plan_id"]),
                "collection_id": row["collection_id"],
                "plan_id": row["plan_id"],
                "intent": {name: row.get(name) for name in _INTENT_FIELDS},
                "evidence": entries,
                "motivation": motivation,
                "divergence": divergence(entries, motivation),
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
        "motivation_consulted": motivation_consulted,
        "motivation_truncated": motivation_truncated,
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
    # Declared-ness is not the predicate. A vault authored before the governed
    # contract may legally declare `motivation` as its own free-text field, and
    # `motivation_is_governed` additionally requires the array form — which is
    # exactly the legacy case it exists to exclude. Reading prose as references
    # would invent counts out of a sentence.
    if planning.motivation_is_governed(manifest):
        columns.append(_MOTIVATION_FIELD)
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


def _supersession(root: Path, identifiers: Sequence[str]) -> dict[str, bool]:
    """Report, per resolvable identity, whether the vault has superseded it.

    An identity is absent from the result whenever it could not be resolved to
    exactly one authorized page — and every way that can happen is the same
    absence. A reference the vault does not hold, one it holds twice, one whose
    page is blocked, and one this reader is not released all leave no trace
    here, so the caller has one outcome to report and the review cannot be used
    to probe for hidden knowledge. That is the same rule `_observe` follows for
    a missing versus a withheld Records collection.

    Resolution runs once for the whole batch and writes nothing: the batch
    primitive never creates or rebuilds the reference sidecar, so a review
    leaves even that derived file byte-identical.
    """
    if not identifiers:
        return {}
    resolved = memory_refs.paths_for_ids_read_only(root, identifiers)
    authorized = record_governance.full_release_filter(root)
    states: dict[str, bool] = {}
    for identifier, paths in resolved.items():
        # Authorization precedes uniqueness, and the order is a disclosure
        # decision rather than a stylistic one. Deciding uniqueness on the
        # unfiltered map makes an unreleased twin observable: the identical
        # reference resolves when no hidden page shares the identity and
        # refuses when one does, so a reader who can cite an id — and needs
        # only one released page of their own to cite it from — learns whether
        # a page they may not read also carries it. Filtering first answers
        # about the pages this reader may actually see, which is both the
        # honest answer and the one that discloses nothing.
        visible = [relative for relative in paths if authorized(relative)]
        if len(visible) != 1:
            continue
        relative = visible[0]
        page = find_corpus.CACHE.get(root / relative, root)
        if page is None:
            continue
        # `status == "superseded"` alone, deliberately. A non-empty
        # `superseded_by` is also true of a hand-edited page whose status was
        # never flipped; reporting that inconsistency is `audit`'s job, not
        # this reader's.
        states[identifier] = page.status == _SUPERSEDED_STATUS
    return states


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
