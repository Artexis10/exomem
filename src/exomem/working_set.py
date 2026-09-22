"""Bounded role lanes and the working-memory packet.

The packet is what the compiler returns instead of a hit list: a small, ordered,
provenance-bearing set of durable facts about the anchors a turn resolved, under
a hard character budget. Everything in it comes from an existing primitive —
semantic units by category, Records collection items, active Planning items,
entity page facets, the typed graph neighbourhood, evidence pointers — so this
module adds a composition rule, not a new retrieval path and certainly not a
model.

Three properties are load-bearing and each is tested directly:

* **Bounded.** Per-role caps and one global `max_chars`. Text that does not
  fit becomes a POINTER, never a truncated half-claim, so an agent that needs it
  can fetch it and one that does not pays a ref.
* **Ordered.** Units before pages, roles in registry priority order. A packet
  that has to be cut keeps the most specific material at the highest-priority
  lens.
* **Honest about lifecycle.** A superseded unit is dropped when its successor is
  in the packet and marked `superseded` with its successor named when it is not.
  It is never presented as current.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from . import (
    context_roles,
    request_budget,
    working_set_index,
    working_set_resolve,
    working_set_state,
)

log = logging.getLogger(__name__)

DEFAULT_BUDGET_CHARS = 4000
MIN_BUDGET_CHARS = 500
MAX_BUDGET_CHARS = 8000
MAX_UNIT_CHARS = 360
MAX_ITEMS_PER_ROLE = 3
MAX_POINTERS = 40
#: Units the unit lane reads before neighbourhood filtering. Generous because the
#: catalogue query is filtered by category and the path filter runs after it.
UNIT_LANE_LIMIT = 200
GRAPH_MAX_NODES = 24
GRAPH_MAX_EDGES = 40
GRAPH_TRAVERSAL_PROFILE = "epistemic"

#: Entries the working-continuity block may carry, and the characters they may
#: spend. Both are hard: the block leads every packet, including an abstained
#: one, so an unbounded one would be paid for by every turn.
RECENT_CONTEXT_MAX_ENTRIES = 8
RECENT_CONTEXT_MAX_CHARS = 900
#: How a page came to be recent. A closed vocabulary, and deliberately about
#: CONTACT rather than meaning: `edited` and `captured` are file changes,
#: `activated` is a read, `planning` is an open commitment. None of them claims
#: the page's subject happened recently — that is what its own content says.
RECENT_CONTEXT_REASONS: tuple[str, ...] = ("edited", "activated", "captured", "planning")

PACKET_BLOCKS = (
    # First, and in resolved and abstained packets alike: a fresh session opens
    # with what was recently worked on, before anything this turn resolved.
    "recent_context",
    "anchors",
    "roles",
    "units",
    "pointers",
    "current_state",
    "missing",
    "ambiguity",
    "budget",
    "generation",
    "abstained",
)


@dataclass(frozen=True, slots=True)
class LaneItem:
    """One lane's candidate, before the budget decides unit or pointer."""

    role: str
    level: str
    ref: str
    path: str
    title: str
    text: str
    lifecycle: str
    updated: str
    anchor: str
    provenance: dict[str, Any] = field(default_factory=dict)
    #: Why this lane admitted the item, in words. Carried onto a pointer so a
    #: ref the budget could not afford still says what it would have answered.
    why: str = ""


class LaneResult(NamedTuple):
    """What one lane produced, and whether it stopped short of the neighbourhood.

    `truncated` is the honest half. A lane that hits its read limit with
    in-neighbourhood units still unread has NOT answered its role, and a packet
    that says nothing about it reads identically to one where the material did
    not exist.
    """

    items: tuple[LaneItem, ...]
    truncated: bool = False


def clamp_budget(value: object) -> int:
    """Clamp a requested budget into the declared range; `None` takes the default."""
    if value is None:
        return DEFAULT_BUDGET_CHARS
    try:
        requested = int(value)
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_CHARS
    return max(MIN_BUDGET_CHARS, min(MAX_BUDGET_CHARS, requested))


def bounded_text(text: str, limit: int = MAX_UNIT_CHARS) -> str:
    """Cut authored prose at a boundary that leaves no unclosed wikilink.

    A naive `text[:360]` produced `... [[norther`, which is unreadable AND
    unscannable: the egress guard recognises a reference by matching `[[…]]`, so
    a cut that orphans the opening brackets hides a withheld page from the scan
    as well as from the reader. Cutting before the orphaned `[[` costs a few
    characters and keeps both properties.
    """
    compact = text.strip()
    if len(compact) <= limit:
        return compact
    cut = compact[:limit]
    opened = cut.rfind("[[")
    if opened != -1 and cut.find("]]", opened) == -1:
        return cut[:opened].rstrip()
    return cut


def graph_depth_for(status: str) -> int:
    """Typed-graph depth by anchor status: 2 for `resolved`, none otherwise.

    No lane runs for a `partial` anchor at all (canonical spec's restated
    "Bounded role lanes" requirement): it is listed in `anchors[]` with its
    status and evidence so the agent can choose it, and served only once it
    resolves or is chosen. There is deliberately no depth-1 case to reach —
    `_neighbourhood_paths` never hands this a `partial` anchor now that
    `compile_packet` builds `run_lanes`' anchor list from `resolved_anchors`
    alone.
    """
    if status == "resolved":
        return 2
    return 0


# --------------------------------------------------------------------------- #
# Packet assembly
# --------------------------------------------------------------------------- #


def _role_order(roles: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {str(role.get("id")): index for index, role in enumerate(roles)}


def build_packet(
    *,
    items: Sequence[LaneItem],
    anchors: Sequence[Mapping[str, Any]],
    roles: Sequence[Mapping[str, Any]],
    current_state: Sequence[Mapping[str, Any]],
    ambiguity: Sequence[Mapping[str, Any]],
    missing: Sequence[Mapping[str, Any]],
    max_chars: int,
    generation: Mapping[str, Any],
    status: str,
    recent_context: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Order, cap and budget the lane output into the packet the caller sees."""
    limit = clamp_budget(max_chars)
    order = _role_order(roles)
    present_paths = {item.path for item in items if item.path}

    def _sort_key(item: LaneItem) -> tuple:
        return (
            0 if item.level == "unit" else 1,
            order.get(item.role, len(order)),
            _lifecycle_rank(item.lifecycle),
            -_date_rank(item.updated),
            item.ref,
        )

    ordered = sorted(items, key=_sort_key)
    units: list[dict[str, Any]] = []
    deferred: list[tuple[LaneItem, str]] = []
    per_role: dict[str, int] = {}

    # Working continuity is budgeted before anything else: it is the block a
    # fresh session opens with, and a turn that resolved a lot must not spend
    # the whole ceiling before saying what was recently worked on.
    recent_entries, used = _budgeted_recent(recent_context, limit)

    # Current state is the highest-value prose about the RESOLVED anchors — it
    # is the answer to "what is true right now" — so it is budgeted next and the
    # rest of the packet spends what is left. An entry that cannot fit is
    # dropped whole: a half-sentence about an observed status is worse than
    # silence.
    state_entries: list[dict[str, Any]] = []
    for entry in current_state:
        statement = str(entry.get("statement") or "")
        if used + len(statement) > limit:
            continue
        state_entries.append(dict(entry))
        used += len(statement)

    for item in ordered:
        if _redundant_superseded(item, present_paths):
            continue
        text = bounded_text(item.text)
        role_count = per_role.get(item.role, 0)
        if role_count >= MAX_ITEMS_PER_ROLE:
            deferred.append((item, "role_cap"))
            continue
        if used + len(text) > limit or not text:
            deferred.append((item, "budget"))
            continue
        units.append(
            {
                "ref": item.ref,
                "role": item.role,
                "text": text,
                "lifecycle": item.lifecycle,
                "updated": item.updated,
                "provenance": _provenance(item),
            }
        )
        used += len(text)
        per_role[item.role] = role_count + 1

    # A pointer is cheap but not free: its title and `why` are prose the caller
    # pays for, so the same ceiling bounds them. Once the budget is spent the
    # overflow is not reported at all rather than silently breaking the bound.
    pointers: list[dict[str, Any]] = []
    # Roles that lost at least one item to the ceiling. An item that fits neither
    # as a unit nor as a pointer leaves no trace otherwise, and a packet that
    # silently drops six of eight candidates reads exactly like one where the
    # material did not exist.
    starved: dict[str, None] = {}
    for item, reason in deferred:
        if len(pointers) >= MAX_POINTERS:
            starved.setdefault(item.role, None)
            continue
        pointer = _pointer(item, reason)
        cost = len(pointer["title"]) + len(pointer["why"])
        if used + cost > limit:
            starved.setdefault(item.role, None)
            continue
        pointers.append(pointer)
        used += cost

    packet: dict[str, Any] = {
        "recent_context": recent_entries,
        "anchors": [dict(anchor) for anchor in anchors],
        "roles": [dict(role) for role in roles],
        "units": units,
        "pointers": pointers,
        "current_state": state_entries,
        "missing": [
            *(dict(entry) for entry in missing),
            *({"role": role, "reason": "budget"} for role in starved),
        ],
        "ambiguity": [dict(entry) for entry in ambiguity],
        "budget": {"limit_chars": limit, "used_chars": used},
        "generation": dict(generation),
        "abstained": status != "resolved",
    }
    if packet["abstained"]:
        packet["abstention"] = {"reason": status}
    return packet


def abstained_packet(
    *,
    reason: str,
    max_chars: int,
    generation: Mapping[str, Any],
    anchors: Sequence[Mapping[str, Any]] = (),
    ambiguity: Sequence[Mapping[str, Any]] = (),
    missing: Sequence[Mapping[str, Any]] = (),
    recent_context: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The packet with no ANSWER in it — but still with working continuity.

    Abstention injects no units, no pointers and no current state: the turn
    resolved nothing, and inventing material for it is exactly what the
    compiler exists not to do. `recent_context` is not material about a
    resolved anchor, though — it is what was recently worked on, which is true
    of the session regardless of what this turn reached, and it is the one
    thing a fresh session opening on "ok continue" has to be told.
    """
    limit = clamp_budget(max_chars)
    recent_entries, used = _budgeted_recent(recent_context, limit)
    return {
        "recent_context": recent_entries,
        "anchors": [dict(anchor) for anchor in anchors],
        "roles": [],
        "units": [],
        "pointers": [],
        "current_state": [],
        "missing": [dict(entry) for entry in missing],
        "ambiguity": [dict(entry) for entry in ambiguity],
        "budget": {"limit_chars": limit, "used_chars": used},
        "generation": dict(generation),
        "abstained": True,
        "abstention": {"reason": reason},
    }


def _budgeted_recent(
    recent_context: Sequence[Mapping[str, Any]], limit: int
) -> tuple[list[dict[str, Any]], int]:
    """The working-continuity entries that fit, and what they cost.

    Two ceilings, both hard: the block's own `RECENT_CONTEXT_MAX_CHARS` and the
    packet's `max_chars`, which it is counted inside rather than added on top
    of. An entry that does not fit is dropped WHOLE — a title cut mid-word, or
    a status sentence with its verb missing, is a claim about recent work that
    nobody can check — and dropping it does not stop a later, shorter entry
    from fitting: unlike the rendered block, this is data with no order the
    reader cuts from the end of, and the caller has already ranked it.
    """
    entries: list[dict[str, Any]] = []
    used = 0
    for entry in recent_context:
        if len(entries) >= RECENT_CONTEXT_MAX_ENTRIES:
            break
        cost = len(str(entry.get("title") or "")) + len(str(entry.get("statement") or ""))
        if used + cost > RECENT_CONTEXT_MAX_CHARS or used + cost > limit:
            continue
        entries.append(dict(entry))
        used += cost
    return entries, used


def _pointer(item: LaneItem, reason: str) -> dict[str, Any]:
    """A ref the budget could not afford, still saying what it would answer.

    `why` is the lane's own words for what admitted it; `reason` is the
    mechanical cause it is a pointer rather than a unit (`budget`, `role_cap`).
    """
    return {
        "ref": item.ref,
        "role": item.role,
        "title": item.title,
        "why": item.why or f"{item.role} lane",
        "reason": reason,
    }


def _provenance(item: LaneItem) -> dict[str, Any]:
    out = {"path": item.path, "level": item.level, "anchor": item.anchor}
    out.update({key: value for key, value in item.provenance.items() if value})
    return out


def _superseded_targets(item: LaneItem) -> tuple[str, ...]:
    raw = item.provenance.get("superseded_by")
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, (list, tuple)):
        return tuple(str(value) for value in raw if isinstance(value, str))
    return ()


def _redundant_superseded(item: LaneItem, present_paths: frozenset[str] | set[str]) -> bool:
    """Drop a superseded item when its own successor is already in the packet."""
    if item.lifecycle != "superseded":
        return False
    return any(target in present_paths for target in _superseded_targets(item))


def _lifecycle_rank(lifecycle: str) -> int:
    return 0 if lifecycle == "active" else 1


def _date_rank(updated: str) -> int:
    digits = "".join(character for character in str(updated) if character.isdigit())
    return int(digits[:8]) if digits else 0


# --------------------------------------------------------------------------- #
# Lanes
# --------------------------------------------------------------------------- #


def run_lanes(
    vault_root: Path,
    *,
    anchors: Sequence[Any],
    roles: Sequence[Mapping[str, Any]],
    registry: context_roles.RoleRegistry,
    current_state: Sequence[Mapping[str, Any]] = (),
    timings: Any = None,
    freshness_snapshot: Any = None,
) -> tuple[tuple[LaneItem, ...], tuple[dict[str, Any], ...]]:
    """Run one bounded lane per selected role. Every lane soft-fails alone.

    `current_state` is resolved ONCE by the caller and handed in, because the
    Records lane and the packet's own `current_state[]` block are two views of
    the same reads and resolving them twice doubled the collection queries.
    """
    root = Path(vault_root)
    items: list[LaneItem] = []
    missing: list[dict[str, Any]] = []
    neighbourhood = _neighbourhood_paths(root, anchors)
    for role in roles:
        role_id = str(role.get("id"))
        definition = registry.roles.get(role_id)
        if definition is None:
            continue
        lane_stage = f"working_set.lanes.{role_id}"
        if budget_exhausted(lane_stage):
            # Discards any items/missing entries already gathered from prior
            # lanes in this same loop: there is no partial packet, only a
            # compiled one or an abstained one, and `compile_packet`'s caller
            # already abstains (not cached) on any exception raised here.
            raise BudgetExhausted(lane_stage)
        failed = False
        result = LaneResult(())
        with _span(timings, lane_stage):
            try:
                result = _lane(
                    root,
                    definition,
                    anchors=anchors,
                    neighbourhood=neighbourhood,
                    current_state=current_state,
                    freshness_snapshot=freshness_snapshot,
                )
            except Exception:  # noqa: BLE001 - one lane's failure is not the packet's
                log.debug("activation lane %s failed", role_id, exc_info=True)
                failed = True
        if failed:
            missing.append({"role": role_id, "reason": "lane_failed"})
        elif not result.items:
            missing.append({"role": role_id, "reason": "no_material"})
        if result.truncated:
            missing.append({"role": role_id, "reason": "lane_truncated"})
        items.extend(result.items)
    return tuple(items), tuple(dict(entry) for entry in missing)


def _neighbourhood_paths(vault_root: Path, anchors: Sequence[Any]) -> frozenset[str]:
    """Anchor paths plus their typed neighbours, at the depth each status allows."""
    paths: set[str] = set()
    for anchor in anchors:
        depth = graph_depth_for(getattr(anchor, "status", "unresolved"))
        if depth <= 0:
            continue
        path = str(getattr(anchor, "path", "") or "")
        if path:
            paths.add(path)
        paths.update(str(item) for item in getattr(anchor, "neighbourhood", ()) or ())
        if depth >= 2:
            paths.update(_graph_neighbours(vault_root, path, depth=depth))
    return frozenset(paths)


def _graph_neighbours(vault_root: Path, path: str, *, depth: int) -> frozenset[str]:
    if not path or not path.endswith(".md"):
        return frozenset()
    try:
        from . import epistemic_graph

        context = epistemic_graph.graph_context(
            vault_root,
            path=path,
            depth=depth,
            max_nodes=GRAPH_MAX_NODES,
            max_edges=GRAPH_MAX_EDGES,
            traversal_profile=GRAPH_TRAVERSAL_PROFILE,
        )
    except Exception:  # noqa: BLE001 - the graph lane is optional by contract
        log.debug("activation graph expansion failed for %s", path, exc_info=True)
        return frozenset()
    if not isinstance(context, Mapping) or not context.get("available", True):
        return frozenset()
    out: set[str] = set()
    for node in context.get("nodes") or ():
        if isinstance(node, Mapping):
            candidate = node.get("path") or node.get("ref")
            if isinstance(candidate, str) and candidate.endswith(".md"):
                out.add(candidate)
    return frozenset(out)


def _lane(
    vault_root: Path,
    role: context_roles.ContextRole,
    *,
    anchors: Sequence[Any],
    neighbourhood: frozenset[str],
    current_state: Sequence[Mapping[str, Any]] = (),
    freshness_snapshot: Any = None,
) -> LaneResult:
    """Dispatch one role to its lane.

    No lane consumes the turn TEXT. Selection already happened — the anchors and
    the role are what the turn produced — and re-ranking a lane by resemblance to
    the turn would reintroduce the failure the compiler exists to fix: a vague
    turn scoring the wrong material highly. The neighbourhood and the category set
    are the selectors, and they are categorical.
    """
    if role.lane == "units":
        return _units_lane(
            vault_root, role, neighbourhood=neighbourhood, freshness_snapshot=freshness_snapshot
        )
    if role.lane == "records":
        return LaneResult(_records_lane(role, current_state=current_state))
    if role.lane == "planning":
        return LaneResult(_planning_lane(role, anchors=anchors))
    if role.lane == "entity":
        return LaneResult(_entity_lane(vault_root, role, anchors=anchors))
    if role.lane == "graph":
        return LaneResult(_graph_lane(role, anchors=anchors))
    if role.lane == "evidence":
        return LaneResult(_evidence_lane(role, neighbourhood=neighbourhood))
    return LaneResult(())


def _units_lane(
    vault_root: Path,
    role: context_roles.ContextRole,
    *,
    neighbourhood: frozenset[str],
    freshness_snapshot: Any = None,
) -> LaneResult:
    """Semantic units by category, restricted to the anchor neighbourhood.

    Both category and parent-path constraints run in the maintained catalogue
    before its bounded read, so unrelated units cannot consume this role's cap.
    """
    if not role.categories or not neighbourhood:
        return LaneResult(())
    from . import find as find_module
    from . import ranking_config, structured_filters
    # One row past the limit, then sliced. Reading exactly `UNIT_LANE_LIMIT` rows
    # cannot distinguish "there was more" from "that was all", so the marker would
    # over-report on a corpus that happens to hold exactly the limit.
    snapshot = freshness_snapshot or find_module.FreshnessSnapshot(Path(vault_root))
    plan = structured_filters.compile_filter(
        None,
        shortcuts=structured_filters.FilterShortcuts(categories=tuple(sorted(role.categories))),
    )
    capped = []
    hits = find_module._find_semantic_units(
        Path(vault_root),
        query="",
        limit=UNIT_LANE_LIMIT + 1,
        scope="kb",
        plan=plan,
        snapshot=snapshot,
        prefer_active=True,
        config=ranking_config.DEFAULT_RANKING,
        mode="keyword",
        degraded_out=None,
        failed_out=None,
        allowed_parent_paths=set(neighbourhood),
        recall_checkpoint=snapshot.recall_checkpoint("kb"),
        repair=False,
        max_catalog_candidates=UNIT_LANE_LIMIT + 1,
        truncated_out=capped,
    )
    truncated = len(hits) > UNIT_LANE_LIMIT or bool(capped)
    hits = hits[:UNIT_LANE_LIMIT]
    out: list[LaneItem] = []
    for hit in hits:
        parent = str(getattr(hit, "parent_path", "") or "")
        superseded_by = list(getattr(hit, "parent_superseded_by", ()) or ())
        lifecycle = "superseded" if superseded_by else "active"
        out.append(
            LaneItem(
                role=role.id,
                level="unit",
                ref=str(getattr(hit, "unit_ref", "") or parent),
                path=parent,
                title=str(getattr(hit, "parent_title", "") or parent),
                text=str(getattr(hit, "content", "") or getattr(hit, "excerpt", "") or ""),
                lifecycle=lifecycle,
                updated=str(getattr(hit, "parent_updated", "") or ""),
                anchor=parent,
                provenance={
                    "category": str(getattr(hit, "category", "") or ""),
                    "kind": str(getattr(hit, "kind", "") or ""),
                    "superseded_by": superseded_by,
                },
                why=role.description or f"{role.id} lane",
            )
        )
    return LaneResult(tuple(out), truncated)


def _records_lane(
    role: context_roles.ContextRole,
    *,
    current_state: Sequence[Mapping[str, Any]] = (),
) -> tuple[LaneItem, ...]:
    """The already-resolved current state, as page-level material.

    Reuses the caller's single resolution rather than querying the collections a
    second time: the packet's `current_state[]` block and this lane are two views
    of the same reads.
    """
    return tuple(
        LaneItem(
            role=role.id,
            level="page",
            ref=f"{entry.get('anchor')}#current",
            path="",
            title=str(entry.get("statement") or "")[:80],
            text=str(entry.get("statement") or ""),
            lifecycle="active",
            updated=str(entry.get("as_of") or ""),
            anchor=str(entry.get("anchor") or ""),
            provenance={"source": str(entry.get("source") or ""), "as_of": entry.get("as_of")},
            why=f"observed state per {entry.get('source') or 'the record'}",
        )
        for entry in current_state
    )


def _planning_lane(
    role: context_roles.ContextRole,
    *,
    anchors: Sequence[Any],
) -> tuple[LaneItem, ...]:
    """Active Planning items already recorded for a resolved plan/project anchor."""
    out: list[LaneItem] = []
    for anchor in anchors:
        if getattr(anchor, "kind", "") != "plan":
            continue
        out.append(
            LaneItem(
                role=role.id,
                level="page",
                ref=str(getattr(anchor, "ref", None) or getattr(anchor, "anchor_id", "")),
                path=str(getattr(anchor, "path", "") or ""),
                title=str(getattr(anchor, "title", "") or ""),
                text=str(getattr(anchor, "title", "") or ""),
                lifecycle=str(getattr(anchor, "lifecycle", "active") or "active"),
                updated="",
                anchor=str(getattr(anchor, "ref", None) or getattr(anchor, "path", "")),
                provenance={"source": "planning"},
                why="already planned for this anchor",
            )
        )
    return tuple(out)


def _entity_lane(
    vault_root: Path,
    role: context_roles.ContextRole,
    *,
    anchors: Sequence[Any],
) -> tuple[LaneItem, ...]:
    """The anchor page's own lede — its identity in its own words."""
    from . import find_corpus

    out: list[LaneItem] = []
    for anchor in anchors:
        rel = str(getattr(anchor, "path", "") or "")
        if not rel.endswith(".md"):
            continue
        page = find_corpus.CACHE.get(Path(vault_root) / rel, Path(vault_root))
        if page is None:
            continue
        frontmatter = page.frontmatter if isinstance(page.frontmatter, dict) else {}
        lede = working_set_index.lede(page.body)
        if not lede:
            continue
        out.append(
            LaneItem(
                role=role.id,
                level="page",
                ref=str(getattr(anchor, "ref", None) or rel),
                path=rel,
                title=str(getattr(anchor, "title", "") or page.title),
                text=lede,
                lifecycle=str(getattr(anchor, "lifecycle", "active") or "active"),
                updated=str(frontmatter.get("updated") or ""),
                anchor=str(getattr(anchor, "ref", None) or rel),
                provenance={"source": "profile"},
                why=role.description or "the anchor page in its own words",
            )
        )
    return tuple(out)


def _graph_lane(role: context_roles.ContextRole, *, anchors: Sequence[Any]) -> tuple[LaneItem, ...]:
    """Typed neighbours as pointers. A neighbour's BODY belongs to another lane."""
    out: list[LaneItem] = []
    for anchor in anchors:
        for neighbour in sorted(getattr(anchor, "neighbourhood", ()) or ())[:GRAPH_MAX_NODES]:
            out.append(
                LaneItem(
                    role=role.id,
                    level="page",
                    ref=str(neighbour),
                    path=str(neighbour),
                    title=str(neighbour).rsplit("/", 1)[-1].removesuffix(".md"),
                    text="",
                    lifecycle="active",
                    updated="",
                    anchor=str(getattr(anchor, "ref", None) or getattr(anchor, "path", "")),
                    provenance={"source": "graph"},
                    why="typed neighbour of a resolved anchor",
                )
            )
    return tuple(out)


def _evidence_lane(
    role: context_roles.ContextRole,
    *,
    neighbourhood: frozenset[str],
) -> tuple[LaneItem, ...]:
    """Evidence as POINTERS only: a proof's body never enters working memory."""
    out: list[LaneItem] = []
    for rel in sorted(neighbourhood):
        if "/Evidence/" not in rel and not rel.startswith("Evidence/"):
            continue
        out.append(
            LaneItem(
                role=role.id,
                level="page",
                ref=rel,
                path=rel,
                title=rel.rsplit("/", 1)[-1].removesuffix(".md"),
                text="",
                lifecycle="active",
                updated="",
                anchor=rel,
                provenance={"source": "evidence"},
                why="preserved evidence in the anchor neighbourhood",
            )
        )
    return tuple(out)


# --------------------------------------------------------------------------- #
# Compilation
# --------------------------------------------------------------------------- #


def _span(timings: Any, name: str):
    """One timing span under the existing collector, or a no-op without one."""
    from . import find_types

    return find_types.timing_span(timings, name)


class BudgetExhausted(RuntimeError):
    """The request budget could not afford the next compilation stage.

    Raised from deep inside `compile_packet`/`run_lanes` rather than
    threaded back up as a return value, so it reaches
    `working_set_runtime.serve`'s existing `except Exception` around
    `compile_packet` — which already abstains with reason `unavailable`
    and, critically, already returns BEFORE the cache write below it, so a
    budget-truncated call is never cached by construction. A dedicated type
    (rather than a bare `RuntimeError`) exists only so a deliberate skip
    reads distinctly from a genuine compilation bug in logs and tracebacks.
    """


def budget_exhausted(stage: str) -> bool:
    """True when the active request budget cannot afford to start `stage`.

    One helper shared by every stage boundary in the activation request path
    — `op_activate_context` calls it directly for its own commands.py-level
    stages (freshness, readiness, lexical, release, guard); `compile_packet`
    and `run_lanes` call it for the stages they own (semantic evidence,
    anchor resolution, role selection, current state, each role lane, packet
    build). Reads the SAME in-flight `RequestBudget` either way, via the
    `request_budget` module's context-local `current()` — there is one
    budget per request regardless of which module asks.

    Gated on `can_afford(request_budget.ACTIVATION_STAGE_RESERVE_SECONDS)`,
    matching every other budget consumer's pattern of a named reserve
    (`can_afford(PACK_RESERVE_SECONDS)` and friends) rather than "any
    positive number of milliseconds": 1ms of remaining budget is enough to
    START a stage but never enough to finish one.

    Records the skip on the budget itself (`note_skipped`), so it is safe to
    call at every boundary without special-casing: `RequestBudget.note_skipped`
    dedupes by name, and every caller of this helper either returns or raises
    immediately on `True`, so control flow never reaches a second boundary
    once the budget is gone — the FIRST stage that could not be afforded is
    the only one ever recorded.
    """
    budget = request_budget.current()
    if budget is None or budget.can_afford(request_budget.ACTIVATION_STAGE_RESERVE_SECONDS):
        return False
    budget.note_skipped(stage)
    return True


def signature_evidence(index: working_set_index.WorkingSetIndex, turn: str):
    """Optional semantic corroboration over anchors, never the recall corpus.

    An unavailable scorer removes one evidence kind, not the structural
    resolver or its release guard. It cannot justify a cached negative result.
    """
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return {}, None, "disabled"
    try:
        from . import embeddings, readiness, runtime_resources

        if readiness.should_defer("embeddings"):
            return {}, None, "warming"
        vectors = index.vectors()
        if not vectors:
            return {}, None, "absent"
        try:
            query_vector = embeddings.embed_query_if_loaded(turn)
        except runtime_resources.ModelBusyError:
            return {}, None, "busy"
        if query_vector is None:
            return {}, None, "unavailable"
        return vectors, query_vector, "ready"
    except Exception:  # noqa: BLE001 - optional scorer failure is explicit
        log.debug("activation signature evidence unavailable", exc_info=True)
        return {}, None, "unavailable"


def compile_packet(
    vault_root: Path,
    *,
    turn: str,
    max_chars: int | None = None,
    purpose: str | None = None,
    timings: Any = None,
    retrieval_paths: frozenset[str] = frozenset(),
    index: working_set_index.WorkingSetIndex | None = None,
    freshness_key: str = "",
    freshness_snapshot: Any = None,
    continuity_refs: frozenset[str] = frozenset(),
    anchor: str | None = None,
) -> dict[str, Any]:
    """Resolve, select, retrieve and budget — the whole compiler in one call.

    `anchor` is the agent's own choice of sense: it replaces resolution outright
    rather than joining it, because the agent is the decider of an ambiguous turn
    and the competing senses are then not candidates at all. `continuity_refs`
    only ever qualifies anchors this turn already reached.
    """
    root = Path(vault_root)
    limit = clamp_budget(max_chars)
    index = index or working_set_index.WorkingSetIndex(root)
    registry = context_roles.load_roles(root)
    # The WHOLE sidecar token, in place of the generation read this used to
    # make. It is the same single sidecar read — `generation()` is
    # `read_meta_token(...)[1]` — and the other two fields are what identify
    # the sidecar that issued the number, which is how the manifest registry
    # tells a rebuilt counter from an older one.
    index_token = index.token()
    generation: dict[str, Any] = {
        "freshness_key": freshness_key,
        "index_generation": index_token[1],
        **registry.generation_block(),
    }

    if budget_exhausted("working_set.semantic"):
        raise BudgetExhausted("working_set.semantic")
    with _span(timings, "working_set.semantic"):
        if anchor:
            vectors, query_vector, semantic_state = {}, None, "agent_choice"
        else:
            vectors, query_vector, semantic_state = signature_evidence(index, turn)
    generation["semantic_evidence"] = semantic_state

    if budget_exhausted("working_set.resolve"):
        raise BudgetExhausted("working_set.resolve")
    with _span(timings, "working_set.resolve"):
        # `analysis` is needed on BOTH branches. The override replaces which
        # anchor the turn is about; it does not replace which lenses the turn asks
        # for, and `context_roles.select_roles` below reads `analysis.text` for its
        # `turn_cue` sources. Deleting it here as unused on the override path would
        # silently narrow an overridden packet to the anchor kind's default roles.
        analysis = working_set_resolve.analyze_turn(turn)
        rows = working_set_resolve.facts_from_rows(index.anchors())
        if anchor:
            chosen = working_set_resolve.override_candidates(rows, anchor)
            # A ref that names no anchor is not a packet with nothing in it: the
            # caller asked about a sense that does not exist here. It abstains,
            # and `op_activate_context` turns that into the one refusal an
            # unknown and a withheld ref share.
            resolution = working_set_resolve.resolve(chosen, turn_tokens=analysis.tokens)
        else:
            candidates = working_set_resolve.candidates_for(
                analysis,
                rows,
                vectors=vectors,
                query_vector=query_vector,
                retrieval_paths=retrieval_paths,
                routing_targets=_routing_targets(
                    root, index_token[1], index_token=index_token
                ),
                used_paths=_used_paths(root, rows),
                term_anchor_counts=index.term_anchor_counts(),
            )
            candidates = working_set_resolve.add_graph_corroboration(
                candidates, retrieval_paths=retrieval_paths
            )
            candidates = working_set_resolve.apply_continuity(candidates, continuity_refs)
            resolution = working_set_resolve.resolve(candidates, turn_tokens=analysis.tokens)

    # BEFORE the resolution branch, and in its own span: working continuity is
    # not material about an anchor this turn reached, so an abstention carries
    # it too. "ok continue" resolves nothing by construction, and a fresh
    # session that receives nothing for it is the whole failure this block
    # exists to fix. Skipped rather than raised when the budget is gone: it is
    # an enrichment, and losing it must not turn an honest `unresolved` into
    # `unavailable`.
    #
    # It takes NO budget boundary of its own, in either direction. Not a
    # recording one (`budget_exhausted`): that marks the stage a request
    # STOPPED at, every caller of it returns or raises immediately, and
    # recording one here took the slot `working_set.roles` needed — the
    # regression `test_budget_exhausted_after_resolve_skips_roles_onward`
    # caught. Not a silent affordability read either: the suite simulates
    # exhaustion by counting reads of `RequestBudget.remaining()`
    # (`_CountdownBudget`), which `can_afford` goes through, so ANY check here
    # shifts the documented pre-lane call count that
    # `test_budget_exhausted_between_two_role_lanes_discards_the_first_lanes_
    # work` pins. What makes that safe is that the block is bounded work
    # between two boundaries that already gate the request — it reads a dict,
    # sorts it, and touches at most `RECENT_CONTEXT_MAX_ENTRIES` cached pages
    # — and an exhausted budget raises at `working_set.roles` immediately
    # below.
    with _span(timings, "working_set.recent"):
        recent: tuple[dict[str, Any], ...] = _recent_context(root, rows=rows)

    if resolution.status != "resolved":
        return abstained_packet(
            reason=resolution.status,
            max_chars=limit,
            generation=generation,
            anchors=tuple(anchor.as_dict() for anchor in resolution.anchors),
            ambiguity=resolution.ambiguity,
            recent_context=recent,
        )

    if budget_exhausted("working_set.roles"):
        raise BudgetExhausted("working_set.roles")
    with _span(timings, "working_set.roles"):
        anchor_kinds = tuple(dict.fromkeys(anchor.kind for anchor in resolution.resolved_anchors))
        roles = context_roles.select_roles(registry, anchor_kinds=anchor_kinds, analysis=analysis)

    # RESOLVED anchors only: a `partial` anchor is listed in `anchors[]` with
    # its status and evidence, but no lane runs for it and nothing of its page
    # or neighbourhood enters `units`, `pointers` or `current_state` (canonical
    # spec's restated "Bounded role lanes" requirement — "no lane SHALL run for
    # a partial anchor").
    lane_anchors = resolution.resolved_anchors
    if budget_exhausted("working_set.current_state"):
        raise BudgetExhausted("working_set.current_state")
    # Resolved ONCE: the Records lane and the packet's `current_state[]` block are
    # two views of the same collection reads.
    with _span(timings, "working_set.current_state"):
        current_state = working_set_state.current_state_for(
            root,
            anchors=resolution.resolved_anchors,
            purpose=purpose,
            index_generation=index_token[1],
            index_token=index_token,
        )
    items, missing = run_lanes(
        root,
        anchors=lane_anchors,
        roles=roles,
        registry=registry,
        current_state=current_state,
        timings=timings,
        freshness_snapshot=freshness_snapshot,
    )

    if budget_exhausted("working_set.budget"):
        raise BudgetExhausted("working_set.budget")
    with _span(timings, "working_set.budget"):
        packet = build_packet(
            items=items,
            anchors=tuple(anchor.as_dict() for anchor in resolution.anchors),
            roles=roles,
            current_state=current_state,
            ambiguity=resolution.ambiguity,
            missing=missing,
            max_chars=limit,
            generation=generation,
            status=resolution.status,
            recent_context=recent,
        )
    return packet


def _routing_targets(
    vault_root: Path,
    index_generation: int,
    *,
    index_token: tuple[int, int, int] | None = None,
) -> tuple[Any, ...]:
    """Records routing targets for `claims_match`, via the existing claims rule.

    Read from the manifests the index update published for this generation, so
    the request path enumerates no directory to build them. These claims are
    RESOLUTION EVIDENCE — they can corroborate that a turn is about an anchor,
    never decide what may be disclosed — so serving them as stale as the index
    means a turn reaches more or less evidence, exactly the contract anchors
    themselves already have. The governed read that current state performs does
    NOT share this staleness: it re-reads its manifest (see
    `working_set_state._governing_manifest`).
    """
    try:
        from . import collection_claims, record_governance

        manifests = working_set_index.records_manifests(
            vault_root, index_generation, token=index_token
        )
    except Exception:  # noqa: BLE001 - no targets simply means no claims evidence
        log.debug("activation: routing targets unavailable", exc_info=True)
        return ()
    targets: list[Any] = []
    for manifest in manifests:
        claims = record_governance.effective_claims(manifest, None)
        if not claims:
            continue
        targets.append(
            collection_claims.RoutingTarget(
                collection=str(getattr(manifest, "path", "")),
                title=str(getattr(manifest, "title", "")),
                claims=claims,
                natural_key=tuple(getattr(getattr(manifest, "schema", None), "natural_key", ())),
            )
        )
    return tuple(targets)


def _used_paths(vault_root: Path, rows: Sequence[Any]) -> frozenset[str]:
    """Anchor paths whose ACT-R usage multiplier is above the neutral floor.

    Tie-break only. `usage_prior` never contributes to the two-kinds rule, so a
    frequently-read page cannot become an anchor the turn never named. Reuses the
    same memoized activation map `find(prefer_used=true)` reads, so "used" means
    exactly what it means in ranking.
    """
    del vault_root
    try:
        from . import ranking_config, usage

        config = ranking_config.DEFAULT_RANKING
        activations = usage.activation_map(config)
    except Exception:  # noqa: BLE001 - the tie-break is optional by construction
        return frozenset()
    if not activations:
        return frozenset()
    out: set[str] = set()
    for row in rows:
        path = str(getattr(row, "path", "") or "")
        if not path:
            continue
        activation = activations.get(usage.canon(path), activations.get(path))
        if activation is None:
            continue
        if usage.usage_multiplier(activation, config) > 1.0:
            out.add(path)
    return frozenset(out)


# --------------------------------------------------------------------------- #
# Working continuity
# --------------------------------------------------------------------------- #


def _recent_context(
    vault_root: Path,
    *,
    rows: Sequence[Any],
    limit: int = RECENT_CONTEXT_MAX_ENTRIES,
) -> tuple[dict[str, Any], ...]:
    """What was recently worked on — the block a turn that resolved nothing
    still carries.

    Four sources, none of which enumerates a directory or walks the vault:

    * the freshness registry's per-path mtimes, which the watcher maintains and
      this reads as a dict (`edited`, or `captured` for a captured session);
    * the memoized ACT-R activation snapshot `_used_paths` already reuses, for
      pages this vault has actually been reading (`activated`);
    * the activation index's own planning rows, for open commitments the
      recent edits did not already surface (`planning`);
    * and, for the CHOSEN entries only, each page's own authored
      `status`/`summary` frontmatter, for the one-line statement.

    The statement is deliberately NOT the current-state resolver, though that
    is where `current_state[]` gets its own. The block's pages are chosen by
    recency, not by the turn, so routing them through a governed collection
    query drags the storage of collections the turn never named onto the
    request path — measured at 9 directory enumerations against a ceiling of
    8, three of them inside an unasked collection
    (`test_a_collection_the_turn_never_named_stays_off_the_request_path`).
    Frontmatter is the same authored value that resolver's own second tier
    reads, and it costs one cached page read.

    Ranked most recent first, deduped by path — a page that was both edited and
    read appears once, under the reason that offered it first — and capped at
    `limit`. `as_of` dates the CONTACT, never the event the page describes: a
    note edited today about a decision taken in March is recent work on an old
    decision, and `why` is what says which.

    Best-effort by construction. Every source is optional and every failure
    costs the block its entries, never the packet.
    """
    root = Path(vault_root)
    by_path: dict[str, Any] = {}
    for row in rows:
        path = str(getattr(row, "path", "") or "")
        if path and path not in by_path:
            by_path[path] = row
    mtimes = _recent_mtimes(root)
    collections = _recent_collection_dirs(mtimes)
    offered: dict[str, str] = {}
    for rel in sorted(mtimes, key=lambda item: (-mtimes[item], item)):
        if len(offered) >= limit:
            break
        why = _recent_reason_for(rel, collections=collections)
        if why and rel not in offered:
            offered[rel] = why
    for rel in _recently_activated(by_path, mtimes, limit=limit, collections=collections):
        offered.setdefault(rel, "activated")
    for rel in _recent_planning(by_path, mtimes, limit=limit, collections=collections):
        offered.setdefault(rel, "planning")

    def _rank(item: tuple[str, str]) -> tuple[int, int, str]:
        return (-mtimes.get(item[0], 0), RECENT_CONTEXT_REASONS.index(item[1]), item[0])

    # One slot is RESERVED for the newest open Planning item. Ranking the
    # whole block by recency buried it every time: an open commitment nobody
    # has touched is by definition older than the edits, so eight fresh pages
    # cut it, `_recent_planning` could never put anything in the block, and
    # the commitment a resumed session most needs reminding of was the one
    # thing guaranteed missing. It is a reservation, not a takeover — the
    # remaining slots stay recency-ranked, and the entry keeps its place in
    # that order rather than being pinned to the front.
    planning_offers = sorted(
        ((path, why) for path, why in offered.items() if why == "planning"), key=_rank
    )
    others = sorted(
        ((path, why) for path, why in offered.items() if why != "planning"), key=_rank
    )
    if planning_offers:
        ranked = sorted([*others[: max(0, limit - 1)], planning_offers[0]], key=_rank)
    else:
        ranked = others[:limit]

    entries: list[dict[str, Any]] = []
    for path, why in ranked:
        row = by_path.get(path)
        kind = str(getattr(row, "kind", "") or "") or "page"
        entries.append(
            {
                "ref": str(getattr(row, "ref", None) or path),
                "path": path,
                # The anchor's own title when the page is one; otherwise the
                # readable filename, which costs no read. Never the page body.
                "title": str(getattr(row, "title", "") or "") or Path(path).stem,
                "kind": kind,
                "why": why,
                "as_of": _recent_as_of(mtimes.get(path)),
            }
        )

    for entry in entries:
        # A captured session carries its title and date only. Its body is raw
        # material: summarising it here would be the server authoring a claim
        # about a conversation nobody has compiled yet.
        if entry["why"] == "captured":
            continue
        statement = _recent_frontmatter_statement(root, entry["path"])
        if statement:
            entry["statement"] = statement
    return _without_collection_echoes(entries, collections)


def _recent_mtimes(vault_root: Path) -> dict[str, int]:
    """`{vault-relative path: mtime_ns}` from the live freshness registry.

    A dict copy, not a walk: the watcher (or the 300 s reconcile) already
    maintains this map, which is precisely why the lexical heal reads it
    instead of re-statting the corpus. A scope that is not live — no watcher,
    or the kill switch — yields nothing, and the block falls back to the
    sources that need no mtime rather than walking to fill it.
    """
    try:
        from . import freshness

        entries = freshness.live_entries(vault_root, "kb")
    except Exception:  # noqa: BLE001 - the registry is optional by construction
        log.debug("recent context: freshness registry unavailable", exc_info=True)
        return {}
    if not entries:
        return {}
    prefix = f"{vault_root}{os.sep}"
    out: dict[str, int] = {}
    for key, signature in entries.items():
        if not key.startswith(prefix) or not key.lower().endswith(".md"):
            continue
        rel = key[len(prefix) :].replace(os.sep, "/")
        try:
            out[rel] = int(signature[0])
        except (IndexError, TypeError, ValueError):
            continue
    return out


def _recent_collection_dirs(mtimes: Mapping[str, int]) -> frozenset[str]:
    """Every directory that holds a `_collection.md`, from the paths in hand.

    Derived from the freshness map rather than by asking the filesystem
    whether a sibling manifest exists: the map already names every page in the
    knowledge base, so this is dict work on the request path.
    """
    marker = "/_collection.md"
    return frozenset(rel[: -len(marker)] for rel in mtimes if rel.endswith(marker))


def _recent_reason_for(rel: str, *, collections: frozenset[str] = frozenset()) -> str:
    """`edited`, `captured`, or `""` for a page that is not working context.

    Raw material and operational state are excluded by the SAME path rules the
    content corpus already uses (`find_corpus.EXCLUDED_DIR_NAMES`,
    `NAVIGATION_BASENAMES`), so nothing here is a second, drifting opinion
    about what counts as a page. Two exclusions carry their own weight:
    `Sources/` is evidence ABOUT work rather than the work, except
    `Sources/Sessions/`, which IS the record of a conversation; and the KB's
    activity log is rewritten by every confirmed write, so without excluding it
    it would be the most recently edited page on every turn and this block
    would say nothing else.
    """
    from . import find_corpus
    from .kbdir import kb_prefix

    if not rel.lower().endswith(".md"):
        return ""
    inner = rel[len(kb_prefix()) :] if rel.startswith(kb_prefix()) else rel
    parts = inner.split("/")
    if any(part.startswith(".") or part in find_corpus.EXCLUDED_DIR_NAMES for part in parts[:-1]):
        return ""
    if parts[-1].casefold() in find_corpus.NAVIGATION_BASENAMES:
        return ""
    if inner.startswith("Evidence/"):
        return ""
    if inner.startswith("Sources/"):
        return "captured" if inner.startswith("Sources/Sessions/") else ""
    if _inside_collection_storage(rel, collections):
        return ""
    return "edited"


def _inside_collection_storage(rel: str, collections: frozenset[str]) -> bool:
    """True for a collection's own stored items — its `Items/`, or a page
    sitting directly beside its manifest.

    A collection's items are its STORAGE, not working context. Writing one
    record touches a file per observation, so on any vault that actually uses
    Records they are permanently the most recently edited pages there are:
    four of eight slots went to one collection's item files, each offering a
    date and a state field in place of a subject. The manifest itself stays —
    it is the thing with a title and a claim — and this never looks at the
    filesystem, only at which directories the freshness map says hold a
    manifest.
    """
    if not collections or rel.endswith("/_collection.md"):
        return False
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    if parent in collections:
        return True
    grandparent = parent.rsplit("/", 1)[0] if "/" in parent else ""
    return bool(grandparent) and grandparent in collections


def _without_collection_echoes(
    entries: Sequence[Mapping[str, Any]], collections: frozenset[str]
) -> tuple[dict[str, Any], ...]:
    """Drop an entry that only repeats its own collection manifest's title.

    A Planning item and the manifest it lives under are one anchor spelled two
    ways — the index gives them the same authored title — so serving both
    spends two of eight slots saying one thing. The manifest is kept, being
    the entry an agent can act on; the echo goes.
    """
    manifests = {
        str(entry.get("path") or "").rsplit("/", 1)[0]: str(entry.get("title") or "").casefold()
        for entry in entries
        if str(entry.get("path") or "").endswith("/_collection.md")
    }
    if not manifests:
        return tuple(dict(entry) for entry in entries)
    out: list[dict[str, Any]] = []
    for entry in entries:
        path = str(entry.get("path") or "")
        title = str(entry.get("title") or "").casefold()
        if path.endswith("/_collection.md"):
            # A manifest is never an echo — least of all of itself, which it
            # matches by construction (same directory, same title).
            out.append(dict(entry))
            continue
        owner = next(
            (
                directory
                for directory in manifests
                if directory in collections and path.startswith(f"{directory}/")
            ),
            None,
        )
        if owner is not None and manifests[owner] == title:
            continue
        out.append(dict(entry))
    return tuple(out)


def _recently_activated(
    by_path: Mapping[str, Any],
    mtimes: Mapping[str, int],
    *,
    limit: int,
    collections: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """The most-activated known pages, from the memoized usage snapshot.

    The candidate set is the paths this request ALREADY holds — the index's
    anchor rows and the freshness map — so the activation map is read as a
    lookup table and never as a list of paths to go and find. A page that has
    been read a lot but is in neither is simply not offered, which is the
    bounded-work price of never walking.
    """
    try:
        from . import ranking_config, usage

        activations = usage.activation_map(ranking_config.DEFAULT_RANKING)
    except Exception:  # noqa: BLE001 - the usage snapshot is optional by construction
        log.debug("recent context: usage activation unavailable", exc_info=True)
        return ()
    if not activations:
        return ()
    scored: list[tuple[float, str]] = []
    for rel in dict.fromkeys((*by_path, *mtimes)):
        if not _recent_reason_for(rel, collections=collections):
            continue
        activation = activations.get(usage.canon(rel), activations.get(rel))
        if activation is None:
            continue
        scored.append((-float(activation), rel))
    scored.sort()
    return tuple(rel for _activation, rel in scored[:limit])


def _recent_planning(
    by_path: Mapping[str, Any],
    mtimes: Mapping[str, int],
    *,
    limit: int,
    collections: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Open Planning items, newest first — the same rows `_planning_lane` reads.

    Read from the anchor rows this request already has, so an open commitment
    is carried without a second query. An item recently edited is offered by
    the edit source first and keeps that reason; what this adds is the open
    item nobody has touched lately, which is exactly the one a resumed session
    forgets.
    """
    plans = [
        path
        for path, row in by_path.items()
        if str(getattr(row, "kind", "")) == "plan"
        and str(getattr(row, "lifecycle", "active") or "active") == "active"
        and _recent_reason_for(path, collections=collections)
    ]
    plans.sort(key=lambda path: (-mtimes.get(path, 0), path))
    return tuple(plans[:limit])


def _recent_frontmatter_statement(vault_root: Path, rel: str) -> str:
    """The page's authored `status`/`summary`, or `""`.

    Bounded to the entries the block already chose — at most
    `RECENT_CONTEXT_MAX_ENTRIES` pages, through the shared page cache, never a
    scan. Authored values only, rendered the way `working_set_state` renders
    them, so nothing here is a sentence the server wrote.
    """
    from . import find_corpus

    try:
        page = find_corpus.CACHE.get(Path(vault_root) / rel, Path(vault_root))
    except Exception:  # noqa: BLE001 - an unreadable page costs its statement only
        log.debug("recent context: page unreadable for %s", rel, exc_info=True)
        return ""
    if page is None:
        return ""
    frontmatter = page.frontmatter if isinstance(page.frontmatter, dict) else {}
    for name in ("status", "summary"):
        value = frontmatter.get(name)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return f"{name}: {str(value).strip()}"[: working_set_state.STATEMENT_MAX_CHARS]
    return ""


def _recent_as_of(mtime_ns: int | None) -> str:
    """The ISO date of the contact, or `""` when this source carries no time."""
    if not mtime_ns:
        return ""
    import datetime as dt

    try:
        return dt.date.fromtimestamp(mtime_ns / 1_000_000_000).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""
