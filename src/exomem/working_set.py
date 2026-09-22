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
from dataclasses import dataclass, field, replace
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

#: How far ahead of the runner-up the top recall hit must be before a packet
#: is carried on its strength alone (design D3). A near tie is not a weaker
#: answer, it is NOISE: two pages within a factor of this of each other say
#: the turn's words are spread across the corpus, and the honest reply to
#: that is the abstention the turn already had. 1.5 is chosen as a
#: separation a genuine single-topic match clears comfortably on the BM25
#: scale while two pages sharing a vocabulary do not.
RETRIEVAL_CARRY_SEPARATION = 1.5
#: The only absolute score the carry consults, and it is a sanity bound
#: rather than a threshold: the catalogue really does return rows scoring
#: 0.0 at corpus scale, and a row the ranking placed at nothing is not a
#: page a turn named.
#:
#: An absolute FLOOR was tried and removed. `-bm25()` is not comparable
#: between corpora, so any number that separated signal from noise on one
#: vault was wrong on another: measured, the same page the same turn names
#: scored 13.16 with no bulk, 6.81 with 200 pages added — below the floor
#: that had been fitted to it, so the turn abstained — and was outranked by
#: an unrelated filler note at 2000. What makes a hit contact is whether
#: the turn used DISTINCTIVE words, which is what the rarity gate below
#: measures; the score is then only good for comparing two hits taken from
#: the one corpus, which is what the separation test does.
RETRIEVAL_CARRY_MIN_SCORE = 0.0

#: How much room the carry asks the request budget for, as a multiple of
#: what the request's first lexical pass measured. The carry runs the same
#: query shape against the same catalogue, so its cost tracks that one; the
#: margin is for a second pass that happens to be a little dearer than the
#: first (measured at 0.7-1.0x across corpus sizes).
RETRIEVAL_CARRY_BUDGET_MULTIPLE = 1.2

#: A stem is DISTINCTIVE when it occurs on no more than this share of the
#: indexed pages. Corpus-relative on purpose: "rare" is a statement about
#: the vault the turn is being answered from, and the same word is a name in
#: one vault and an everyday word in another.
RETRIEVAL_CARRY_RARE_FRACTION = 0.005
#: The floor under that share. Half a percent of a forty-page vault rounds
#: to nothing, and a cap of zero would make every word distinctive.
RETRIEVAL_CARRY_RARE_MIN_DOCS = 3
#: How many distinctive stems a hit must share with the turn before it is a
#: candidate at all. One is a coincidence at corpus scale; the same two-fact
#: standard the resolver's own `rare_term` clause applies to an anchor.
RETRIEVAL_CARRY_MIN_RARE_TERMS = 2

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
    freshness_snapshot: Any = None,
    neighbourhood: frozenset[str] | None = None,
) -> tuple[tuple[LaneItem, ...], tuple[dict[str, Any], ...]]:
    """Run one bounded lane per selected role. Every lane soft-fails alone.

    `current_state` is resolved ONCE by the caller and handed in, because the
    Records lane and the packet's own `current_state[]` block are two views of
    the same reads and resolving them twice doubled the collection queries.

    `neighbourhood` is normally derived from the anchors' own typed graph.
    A retrieval-carried packet passes its own — the single page recall
    dominated on, and nothing else — because a carried page is not an anchor:
    it has no indexed neighbourhood to expand, and expanding it would spend
    graph work to widen a claim that rests on one recall score.
    """
    root = Path(vault_root)
    items: list[LaneItem] = []
    missing: list[dict[str, Any]] = []
    if neighbourhood is None:
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


def budget_exhausted(stage: str, *, reserve: float | None = None) -> bool:
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

    `reserve` overrides that flat second for a stage whose cost this request
    has already MEASURED. Nothing interrupts a stage once it has started, so
    a flat reserve is only honest for stages that cost about the same every
    time; the carry's query is the same shape as the first lexical pass and
    costs about as much, so on a request where that pass took three seconds
    the flat second admits a second three-second stage and the door budget
    is overshot. A measured reserve refuses it instead.

    Records the skip on the budget itself (`note_skipped`), so it is safe to
    call at every boundary without special-casing: `RequestBudget.note_skipped`
    dedupes by name, and every caller of this helper either returns or raises
    immediately on `True`, so control flow never reaches a second boundary
    once the budget is gone — the FIRST stage that could not be afforded is
    the only one ever recorded.
    """
    budget = request_budget.current()
    needed = (
        request_budget.ACTIVATION_STAGE_RESERVE_SECONDS
        if reserve is None
        else max(request_budget.ACTIVATION_STAGE_RESERVE_SECONDS, float(reserve))
    )
    if budget is None or budget.can_afford(needed):
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


# --------------------------------------------------------------------------- #
# Retrieval-carried packets (design D3)
# --------------------------------------------------------------------------- #


def rare_document_cap(corpus_pages: int) -> int:
    """The document frequency at or below which a stem counts as DISTINCTIVE
    in a corpus of `corpus_pages` indexed pages.

    `max(RETRIEVAL_CARRY_RARE_MIN_DOCS, ceil(share * pages))`. Corpus-relative
    because that is the only way the judgement survives a growing vault: a
    fixed cap is the same mistake a fixed score floor was.
    """
    pages = max(0, int(corpus_pages))
    return max(RETRIEVAL_CARRY_RARE_MIN_DOCS, -(-pages * 5 // 1000))


def dominant_carry(hits: Sequence[tuple[str, float]]) -> tuple[str, float] | None:
    """The one clearly dominant hit among `hits`, or `None`.

    `hits` has already passed the rarity gate, so every entry shares at least
    `RETRIEVAL_CARRY_MIN_RARE_TERMS` DISTINCTIVE stems with the turn — the
    "did this turn name this page" question is answered before this function
    is reached, and answering it is what makes a score usable at all.

    What is left is a comparison between two hits drawn from the one corpus:
    the top must stand `RETRIEVAL_CARRY_SEPARATION` times clear of the next,
    or the turn's distinctive words are spread over the corpus and choosing
    between two close pages is exactly the guess the compiler exists not to
    make. A lone survivor IS dominant — it already carries the two
    distinctive stems, and there is no second page to be confused with.
    `RETRIEVAL_CARRY_MIN_SCORE` is the one absolute left, and it only
    refuses a row the ranking placed at nothing.
    """
    if not hits:
        return None
    path, score = hits[0]
    if score <= RETRIEVAL_CARRY_MIN_SCORE:
        return None
    if len(hits) > 1 and score < RETRIEVAL_CARRY_SEPARATION * hits[1][1]:
        return None
    return str(path), float(score)


def _carry_by_retrieval(
    vault_root: Path,
    *,
    turn: str,
    timings: Any = None,
    freshness_snapshot: Any = None,
    lexical_seconds: float = 0.0,
) -> tuple[str, float] | None:
    """One scored recall over the compiled knowledge base, then the dominance
    test. `None` means carry nothing — the turn abstains exactly as it did.

    Cost falls only on turns that would otherwise have returned an empty
    packet, and it is still refused outright when the request budget has run
    out or the lexical catalogue is anything other than `available`: a
    carried packet rests entirely on recall, so recall that cannot prove
    itself current is no ground to serve one from.

    `lexical_seconds` is what the request's FIRST lexical pass actually
    took. This query is the same shape against the same catalogue, so it
    will cost about the same, and the budget is asked for
    `RETRIEVAL_CARRY_BUDGET_MULTIPLE` times that rather than the flat stage
    reserve — a reserve that cannot pay for the stage it admits is not a
    reserve. Refusing here abstains `unresolved`, which is what the turn did
    before the carry existed; letting it start and run out mid-flight
    abstains `unavailable`, which renders nothing and reads as a fault.
    """
    if budget_exhausted(
        "working_set.carry", reserve=RETRIEVAL_CARRY_BUDGET_MULTIPLE * max(0.0, lexical_seconds)
    ):
        return None
    freshness = None
    recall_checkpoint = None
    if freshness_snapshot is not None:
        try:
            freshness = freshness_snapshot.for_scope("kb")
            recall_checkpoint = freshness_snapshot.recall_checkpoint("kb")
        except Exception:  # noqa: BLE001 - an unreadable snapshot carries nothing
            log.debug("activation carry freshness unavailable", exc_info=True)
            return None
    with _span(timings, "working_set.carry"):
        from . import working_set_runtime

        hits, state = working_set_runtime.carry_candidates(
            vault_root,
            turn,
            freshness=freshness,
            recall_checkpoint=recall_checkpoint,
        )
    if state != "available":
        return None
    return dominant_carry(hits)


def _carry_roles(
    registry: context_roles.RoleRegistry, analysis: Any
) -> tuple[dict[str, str], ...]:
    """The lenses a carried page is read through.

    A carried page is not an anchor and has no anchor KIND, so
    `context_roles.select_roles`' anchor defaults have nothing to key on. The
    LANE is the selector instead: every `units` role, because units are the
    only thing a page by itself can answer with — a Records lane needs a
    collection, a planning lane a plan, an entity lane a profile, and a
    carried page is none of those. The turn's own cues order first so a turn
    asking about constraints gets constraints ahead of preferences, and the
    same `MAX_SELECTED_ROLES` ceiling an ordinary packet has applies here.
    """
    text = str(getattr(analysis, "text", "") or "")
    cued: list[tuple[int, Any]] = []
    for role in registry.roles.values():
        if role.lane != "units":
            continue
        matched = bool(role.cues) and any(cue in text for cue in role.cues)
        cued.append((0 if matched else 1, role))
    cued.sort(key=lambda entry: (entry[0], entry[1].priority))
    return tuple(
        {
            "id": role.id,
            "source": "turn_cue" if rank == 0 else "retrieval_carried",
            "lane": role.lane,
        }
        for rank, role in cued[: context_roles.MAX_SELECTED_ROLES]
    )


def _carried_packet(
    vault_root: Path,
    *,
    page: tuple[str, float],
    analysis: Any,
    registry: context_roles.RoleRegistry,
    limit: int,
    purpose: str | None,
    timings: Any,
    generation: dict[str, Any],
    index_token: tuple[int, int, int],
    freshness_snapshot: Any,
) -> dict[str, Any]:
    """One packet compiled from a single dominant page, marked as carried.

    The page is reported as ONE anchor entry of kind `page` at status
    `retrieval_carried`, whose only evidence is `retrieval`. That spelling is
    the whole honesty of the feature: a reader can tell at a glance that no
    anchor was named and that recall alone put this material here, and
    `mint_continuity` — which carries `resolved` anchors only — declines to
    mint a token from it without needing to know the feature exists.

    Title and lifecycle are taken from the units the lane actually read off
    that page, never asserted: the unit rows already carry their parent
    page's own title and supersession, so the anchor entry says what the
    page says about itself, and falls back to the path when the lane found
    nothing to read.

    The carried page is the packet's ONLY anchor, and that is load-bearing
    rather than incidental. An `unresolved` abstention lists the turn's
    `partial` candidates in `anchors[]` so the agent can choose one; carrying
    them here as well would break the egress rule that makes a withheld
    carried page safe — `guard_working_set` turns a packet into a `withheld`
    abstention only when EVERY anchor was withheld, so surviving partials
    would leave the packet claiming it resolved something after the one page
    it was built from was removed. The cost is that a carried turn no longer
    shows that menu; the material it shows instead is the trade.
    """
    path, _score = page
    roles = _carry_roles(registry, analysis)
    carried = working_set_resolve.ResolvedAnchor(
        anchor_id=path,
        path=path,
        ref=None,
        title=path,
        kind="page",
        lifecycle="active",
        status=working_set_resolve.RETRIEVAL_CARRIED_STATUS,
        evidence=("retrieval",),
        categories=(),
        neighbourhood=frozenset({path}),
    )

    if budget_exhausted("working_set.current_state"):
        raise BudgetExhausted("working_set.current_state")
    with _span(timings, "working_set.current_state"):
        # `page` is not a stateful kind, so this resolves to nothing today.
        # Called anyway rather than skipped: the packet's `current_state[]`
        # block is the one place a stateful carried page would have to
        # appear, and a silent omission here would be the kind of gap that
        # only shows up once `STATEFUL_KINDS` grows.
        current_state = working_set_state.current_state_for(
            vault_root,
            anchors=(carried,),
            purpose=purpose,
            index_generation=index_token[1],
            index_token=index_token,
        )
    items, missing = run_lanes(
        vault_root,
        anchors=(carried,),
        roles=roles,
        registry=registry,
        current_state=current_state,
        timings=timings,
        freshness_snapshot=freshness_snapshot,
        neighbourhood=frozenset({path}),
    )
    for item in items:
        if item.path == path:
            carried = replace(
                carried, title=item.title or path, lifecycle=item.lifecycle or "active"
            )
            break

    generation = {**generation, "carried_by": "retrieval"}
    if budget_exhausted("working_set.budget"):
        raise BudgetExhausted("working_set.budget")
    with _span(timings, "working_set.budget"):
        # `status` is `build_packet`'s own "did this turn produce material"
        # flag — the one thing it decides `abstained` from — not
        # `resolution.status`, which stayed `unresolved` and is why this
        # packet exists at all. What the turn actually did is in the anchor's
        # own `retrieval_carried` status and in `generation.carried_by`.
        return build_packet(
            items=items,
            anchors=(carried.as_dict(),),
            roles=roles,
            current_state=current_state,
            ambiguity=(),
            missing=missing,
            max_chars=limit,
            generation=generation,
            status="resolved",
        )


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
    lexical_seconds: float = 0.0,
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

    # Design D3, and ONLY here: the turn reached no anchor at all. An
    # `ambiguous` turn is untouched (it reached two, and picking between them
    # is the agent's job), an `agent_choice` turn is untouched (a ref that
    # named nothing must keep abstaining `unresolved`, which is what
    # `op_activate_context` turns into its one refusal), and a turn that
    # resolved anything never reaches this line. Carrying is a PACKET-level
    # decision taken after resolution has already abstained — it adds no
    # evidence kind, changes no status rule, and can never resolve an anchor.
    if resolution.status == "unresolved" and not anchor:
        carried = _carry_by_retrieval(
            root,
            turn=turn,
            timings=timings,
            freshness_snapshot=freshness_snapshot,
            lexical_seconds=lexical_seconds,
        )
        if carried is not None:
            return _carried_packet(
                root,
                page=carried,
                analysis=analysis,
                registry=registry,
                limit=limit,
                purpose=purpose,
                timings=timings,
                generation=generation,
                index_token=index_token,
                freshness_snapshot=freshness_snapshot,
            )

    if resolution.status != "resolved":
        return abstained_packet(
            reason=resolution.status,
            max_chars=limit,
            generation=generation,
            anchors=tuple(anchor.as_dict() for anchor in resolution.anchors),
            ambiguity=resolution.ambiguity,
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
