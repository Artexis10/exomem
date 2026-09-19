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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NamedTuple

from . import (
    activation_conventions,
    context_roles,
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

PACKET_BLOCKS = (
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

    # Current state is the highest-value prose in the packet — it is the answer
    # to "what is true right now" — so it is budgeted FIRST and the rest of the
    # packet spends what is left. An entry that cannot fit is dropped whole: a
    # half-sentence about an observed status is worse than silence.
    state_entries: list[dict[str, Any]] = []
    used = 0
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
) -> dict[str, Any]:
    """The empty packet. Abstention injects NOTHING — that is the whole point."""
    return {
        "anchors": [dict(anchor) for anchor in anchors],
        "roles": [],
        "units": [],
        "pointers": [],
        "current_state": [],
        "missing": [dict(entry) for entry in missing],
        "ambiguity": [dict(entry) for entry in ambiguity],
        "budget": {"limit_chars": clamp_budget(max_chars), "used_chars": 0},
        "generation": dict(generation),
        "abstained": True,
        "abstention": {"reason": reason},
    }


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
        failed = False
        result = LaneResult(())
        with _span(timings, f"working_set.lanes.{role_id}"):
            try:
                result = _lane(
                    root,
                    definition,
                    anchors=anchors,
                    neighbourhood=neighbourhood,
                    current_state=current_state,
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
) -> LaneResult:
    """Dispatch one role to its lane.

    No lane consumes the turn TEXT. Selection already happened — the anchors and
    the role are what the turn produced — and re-ranking a lane by resemblance to
    the turn would reintroduce the failure the compiler exists to fix: a vague
    turn scoring the wrong material highly. The neighbourhood and the category set
    are the selectors, and they are categorical.
    """
    if role.lane == "units":
        return _units_lane(vault_root, role, neighbourhood=neighbourhood)
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
) -> LaneResult:
    """Semantic units by category, restricted to the anchor neighbourhood.

    The category set is pushed down into the existing unit catalogue query; the
    neighbourhood restriction is applied to what comes back, because the unit
    lane's filter vocabulary has no page-path axis and inventing one would move a
    retrieval primitive for a composition concern.

    That ordering has a cost worth naming: the read limit applies BEFORE the path
    filter, so a vault with more than `UNIT_LANE_LIMIT` units in this role's
    categories can leave in-neighbourhood units unread. The lane reports that as
    `lane_truncated` rather than letting the packet imply it read everything.
    """
    if not role.categories or not neighbourhood:
        return LaneResult(())
    from . import find as find_module
    from . import ranking_config, structured_filters

    plan = structured_filters.compile_filter(
        None,
        shortcuts=structured_filters.FilterShortcuts(categories=tuple(sorted(role.categories))),
    )
    # One row past the limit, then sliced. Reading exactly `UNIT_LANE_LIMIT` rows
    # cannot distinguish "there was more" from "that was all", so the marker would
    # over-report on a corpus that happens to hold exactly the limit.
    hits = find_module._find_semantic_units(
        Path(vault_root),
        query="",
        limit=UNIT_LANE_LIMIT + 1,
        scope="kb",
        plan=plan,
        snapshot=find_module.FreshnessSnapshot(Path(vault_root)),
        prefer_active=True,
        config=ranking_config.DEFAULT_RANKING,
        mode="keyword",
        degraded_out=None,
        failed_out=None,
    )
    truncated = len(hits) > UNIT_LANE_LIMIT
    hits = hits[:UNIT_LANE_LIMIT]
    out: list[LaneItem] = []
    for hit in hits:
        parent = str(getattr(hit, "parent_path", "") or "")
        if parent not in neighbourhood:
            continue
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


def _graph_lane(
    role: context_roles.ContextRole, *, anchors: Sequence[Any]
) -> tuple[LaneItem, ...]:
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
    conventions_registry = activation_conventions.load_conventions(root)
    conventions = conventions_registry.conventions
    generation: dict[str, Any] = {
        "freshness_key": freshness_key,
        "index_generation": index.generation(),
        **registry.generation_block(),
        **conventions_registry.generation_block(),
    }

    with _span(timings, "working_set.resolve"):
        # `analysis` is needed on BOTH branches. The override replaces which
        # anchor the turn is about; it does not replace which lenses the turn asks
        # for, and `context_roles.select_roles` below reads `analysis.text` for its
        # `turn_cue` sources. Deleting it here as unused on the override path would
        # silently narrow an overridden packet to the anchor kind's default roles.
        analysis = working_set_resolve.analyze_turn(turn)
        rows = working_set_resolve.facts_from_rows(index.anchors())
        if anchor:
            chosen = working_set_resolve.override_candidate(rows, anchor)
            # A ref that names no anchor is not a packet with nothing in it: the
            # caller asked about a sense that does not exist here. It abstains,
            # and `op_activate_context` turns that into the one refusal an
            # unknown and a withheld ref share.
            resolution = working_set_resolve.resolve(
                (chosen,) if chosen is not None else ()
            )
        else:
            candidates = working_set_resolve.candidates_for(
                analysis,
                rows,
                retrieval_paths=retrieval_paths,
                routing_targets=_routing_targets(root),
                used_paths=_used_paths(root, rows),
                term_anchor_counts=index.term_anchor_counts(),
                stopwords=conventions.stopwords,
                rare_term_max_anchors=conventions.rare_term_max_anchors,
            )
            candidates = working_set_resolve.add_graph_corroboration(
                candidates, retrieval_paths=retrieval_paths
            )
            candidates = working_set_resolve.apply_continuity(
                candidates, continuity_refs
            )
            resolution = working_set_resolve.resolve(candidates)

    if resolution.status != "resolved":
        return abstained_packet(
            reason=resolution.status,
            max_chars=limit,
            generation=generation,
            anchors=tuple(anchor.as_dict() for anchor in resolution.anchors),
            ambiguity=resolution.ambiguity,
        )

    with _span(timings, "working_set.roles"):
        anchor_kinds = tuple(
            dict.fromkeys(anchor.kind for anchor in resolution.resolved_anchors)
        )
        roles = context_roles.select_roles(
            registry, anchor_kinds=anchor_kinds, analysis=analysis
        )

    # RESOLVED anchors only: a `partial` anchor is listed in `anchors[]` with
    # its status and evidence, but no lane runs for it and nothing of its page
    # or neighbourhood enters `units`, `pointers` or `current_state` (canonical
    # spec's restated "Bounded role lanes" requirement — "no lane SHALL run for
    # a partial anchor").
    lane_anchors = resolution.resolved_anchors
    # Resolved ONCE: the Records lane and the packet's `current_state[]` block are
    # two views of the same collection reads.
    current_state = working_set_state.current_state_for(
        root,
        anchors=resolution.resolved_anchors,
        purpose=purpose,
        state_fields=conventions.state_fields,
        date_fields=conventions.date_fields,
    )
    items, missing = run_lanes(
        root,
        anchors=lane_anchors,
        roles=roles,
        registry=registry,
        current_state=current_state,
        timings=timings,
    )

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
        )
    return packet


def _routing_targets(vault_root: Path) -> tuple[Any, ...]:
    """Records routing targets for `claims_match`, via the existing claims rule."""
    try:
        from . import collection_claims, record_governance, structured_collections

        manifests = structured_collections.discover_collections(vault_root)
    except Exception:  # noqa: BLE001 - no targets simply means no claims evidence
        log.debug("activation: routing targets unavailable", exc_info=True)
        return ()
    targets: list[Any] = []
    for manifest in manifests:
        if str(getattr(manifest, "semantic_profile", "")) != "records":
            continue
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
