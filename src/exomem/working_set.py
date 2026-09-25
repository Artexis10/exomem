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
import posixpath
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, NamedTuple

from . import (
    context_roles,
    request_budget,
    source_taxonomy,
    working_set_heat,
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
#: `episode` is a conversation's recorded recap, `activated` is a read,
#: `planning` is an open commitment. None of them claims the page's subject
#: happened recently — that is what its own content says. The order is only the
#: equal-mtime tie-break.
RECENT_CONTEXT_REASONS: tuple[str, ...] = (
    "edited",
    "episode",
    "activated",
    "captured",
    "planning",
)
#: Episodes the block may carry, whatever else is recent. One slot is reserved
#: for the newest (a conversation that saved nothing else must still reach the
#: next session), and a burst of conversations takes at most this many, so at
#: least three slots stay for edits and reads and one for an open commitment.
RECENT_EPISODES_MAX = 4
#: Reasons that each hold one slot for their newest offer.
_RESERVED_RECENT_REASONS: tuple[str, ...] = ("planning", "episode", "activated")

#: How many anchors may share the top of the hot profile before it is cut.
#: The profile is what a REFERENTIAL turn resolves against (design §8's
#: amendment), and a turn that names nothing refers to one thing, so the
#: normal size of this set is one: a tie needs equality on all three ranked
#: components at once. That happens whenever the edit time cannot separate
#: anchors — none has one, or all of them fell in one write burst (see
#: `HOT_PROFILE_BURST_PAGES`) — and the reads that order what is left are
#: equal too: a vault nobody has read yet, or one whose pages were all read
#: together. The bound keeps such a turn to a menu of five rather than the
#: whole catalogue. It never selects among a wider tie on the merits, because
#: there are none to select on: `resolve()` hands the tie to the agent.
HOT_PROFILE_K = 5

#: A write burst: a chain of this many pages or more whose last edits follow
#: one another with no gap longer than `HOT_PROFILE_BURST_GAP_NS`. A burst is
#: a batch — a maintenance pass, an import, a sync — not the user's work, and
#: a batch rewrites pages nobody chose. Its edit times say only that the batch
#: ran, so an anchor in one carries no edit signal in the hot profile and is
#: ordered by its reads instead. And so does every edit OLDER than the latest
#: burst: a batch may have rewritten the page the user was working on, taking
#: its signal with it, and then the freshest page left outside the batch is
#: only the freshest survivor — two days old, in the measured case — not the
#: user's last work. Only an edit made after the latest batch still says what
#: the user was doing.
#:
#: A chain, not a fixed window: on a loaded machine a batch stalled 2.9 s
#: between two pages, and a window of one second cut the two pages after the
#: stall off the batch, where the newest of them became the referent. Each
#: gap is measured to the next edit alone, so a single stall no longer
#: splits the run. The cost is known: an agent writing three notes in quick
#: succession is a burst too, and loses its edit signal; that turn falls
#: through to what was read, or abstains, and is never served wrong material.
#:
#: Counted over every page the freshness registry holds, not over anchors
#: alone: a real batch interleaves anchors with ordinary notes. Navigation
#: pages are left out of the count, because every ordinary write also
#: rewrites the activity log and the index, and those must not turn the
#: user's own single edit into a "burst".
HOT_PROFILE_BURST_PAGES = working_set_heat.BURST_PAGES
HOT_PROFILE_BURST_GAP_NS = working_set_heat.BURST_GAP_NS

#: How many ranked rows the carry asks for before it filters. The ranking
#: limit truncated BEFORE raw material and retired pages were dropped, so a
#: run of sources at the head could hide a second compiled page and turn
#: "two named pages, abstain" into "one named page, carry" — the count the
#: whole decision rests on, decided by where the LIMIT happened to fall.
#: Ten leaves room for a page's raw-material twins and its predecessor
#: ahead of it; the packet's own material stays bounded by the unit lanes,
#: which this does not touch. It is a FLOOR rather than the whole answer —
#: see `carry_fetch_size`, since how many rows the gate can admit grows
#: with the corpus and the window has to stay ahead of it.
RETRIEVAL_CARRY_FETCH = 10

#: There is deliberately no separation constant. One existed — the top hit
#: had to stand 1.5x clear of the runner-up — from when the candidate list
#: was everything recall returned and the gap was the only thing telling a
#: match from its neighbours. The naming gate now decides membership, so
#: every row that survives it is a page the turn NAMED, and a gap between
#: two named pages says nothing about which one was meant: measured, one
#: turn naming two pages scored them 19.60 against 19.16, and another
#: differing only in wording scored 31.07 against 17.40. A packet is
#: carried when exactly one page is named; two named pages abstain, and the
#: client can ask which.
#:
#: The only absolute score the carry consults, and it is a sanity bound
#: rather than a threshold: the catalogue really does return rows scoring
#: 0.0 at corpus scale, and a row the ranking placed at nothing is not a
#: page a turn named.
#:
#: One, not zero. "Greater than zero" waved through 3e-06 — what a
#: double-stemmed query scored for a page that scores 25.2 when asked
#: properly — and every comparison downstream then treated that as a real
#: number. A genuine match scores several units even on the thinnest
#: contact this gate admits: 6.36 for a turn that reached its page on two
#: stable terms out of four, 12 to 25 for an ordinary named page, 18.5 for
#: the coincidence the proximity window now refuses. Nothing measured
#: anywhere in this work lands between 0 and 1, which is what keeps this a
#: sanity bound rather than the corpus-dependent floor it replaced.
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
RETRIEVAL_CARRY_MIN_SCORE = 1.0

#: How much room the carry asks the request budget for, as a multiple of
#: what the request's first lexical pass measured. The carry's own cost
#: tracks that pass — same catalogue, same query shape — but it is TWO
#: round trips rather than one, because a rare-term pass runs before the
#: ranking pass.
#:
#: Measured against that first pass at zero, two hundred and two thousand
#: added pages: 0.9x, 1.1x and 1.9x on a quiet machine, and 2.0x, 2.3x and
#: 1.5x for the same tip under load. The reserve covers the dearest of
#: those rather than the typical one, because the two outcomes are not
#: symmetric: a carry that runs past its reserve overshoots the door budget
#: and returns `unavailable`, which renders nothing and reads to the client
#: as a fault, where refusing returns the same empty packet honestly and
#: sooner. What it costs when it fires wrongly is no carry while the first
#: pass sits between about 1.7 and 2.0 seconds — a band U4's lexical-stage
#: work is about to shrink from the other side.
RETRIEVAL_CARRY_BUDGET_MULTIPLE = 2.5

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
#: How close two of a page's distinctive words must sit in the turn before
#: they read as a NAME rather than as two things the speaker mentioned.
#:
#: Rarity alone says a word is name-shaped; it cannot say the turn used it
#: to name this page. Measured on a 235-page corpus whose prose uses every
#: everyday word of the turn: "I am flying to lisbon next week and wanted to
#: walk around the harbour if there is time" shares `lisbon` and `harbour`
#: with a page about a harbour ledger and a lisbon freight window, both
#: genuinely distinctive, and carried it at 18.51. Nine tokens apart in an
#: ordinary sentence they are two things the speaker mentioned. Four tokens
#: is a phrase — "kelvane throughput ceiling", "quillon vantry window" —
#: with room for the article or preposition a phrase carries.
RETRIEVAL_CARRY_RARE_WINDOW = 4
#: There is deliberately no "N distinctive stems anywhere" path. One
#: existed — three of a page's distinctive words, wherever they sat, named
#: it — on the reasoning that a turn does not land on three by accident.
#: Measured, it does: a long travel sentence mentioning three place names
#: about forty tokens apart carried a freight rota that lists all three, at
#: 26.19 and alone. A page that enumerates many things contains any few of
#: them, and scattering is exactly what tells a list from a name. One
#: admission path, a phrase; a third rare stem may raise the score but
#: never admits.
#: The smallest corpus the rarity gate may be believed on. Below it the
#: carry does not run and the turn abstains as it did before.
#:
#: Rarity is only as sharp as the corpus it is measured against, and below
#: this size there is no corpus to measure against. On a thirty-page vault
#: where the word "meeting" appears on exactly one page, "meeting" IS rare
#: by measurement — and so is every other ordinary English word, because
#: the vault holds no ordinary prose for them to be ordinary in. Measured:
#: a two-line note titled "Meeting notes" whose one unit read "Decision
#: pending" was carried at 12.02 for an ordinary turn about a meeting and a
#: pending decision, and the same stub is refused the moment the corpus
#: contains ordinary notes.
#:
#: A floor on the CORPUS rather than on the turn, deliberately. "A turn all
#: of whose words are rare tells you nothing" would have closed the same
#: case and would also reject a short turn made entirely of real names,
#: which is the turn this feature exists to serve.
RETRIEVAL_CARRY_MIN_PAGES = 100

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


def _deduplicated(ordered: Sequence[LaneItem]) -> tuple[LaneItem, ...]:
    """One item per `ref`, keeping the first in the order given.

    An empty ref is not an identity, so those are all kept: two lanes with
    nothing to name themselves by are not evidently the same material.
    """
    seen: set[str] = set()
    out: list[LaneItem] = []
    for item in ordered:
        ref = str(item.ref or "")
        if ref:
            if ref in seen:
                continue
            seen.add(ref)
        out.append(item)
    return tuple(out)


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

    # One unit, once. Role categories overlap by design — `recent_change`
    # and `precedents` both select `decision`, `resources` and `baseline`
    # both select `fact` — so two selected roles routinely read the SAME
    # unit off the same page, and a retrieval-carried packet, which selects
    # every units role, reads a page's units several times over. Served
    # twice it is one sentence printed twice in the agent's context and
    # charged twice against the character budget.
    #
    # Deduped HERE rather than in either lane, so every packet benefits and
    # no future lane has to remember. After `_sort_key`, so the winner is
    # the first role in registry priority order that reached it — the most
    # specific lens that asked. A `level`-less or ref-less item is left
    # alone: its identity is not its ref.
    ordered = _deduplicated(sorted(items, key=_sort_key))
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

    Two ceilings, both hard: the block's own `RECENT_CONTEXT_MAX_CHARS` and
    HALF the packet's `max_chars`, which it is counted inside rather than added
    on top of. The half is what keeps it a block rather than the packet: at the
    budget floor it was spending 500 characters of 500, so a caller who asked
    for a small packet got working continuity and no answer at all. Leading the
    packet is not the same as being it.

    An entry that does not fit is dropped WHOLE — a title cut mid-word, or a
    status sentence with its verb missing, is a claim about recent work that
    nobody can check — and dropping it does not stop a later, shorter entry
    from fitting: unlike the rendered block, this is data with no order the
    reader cuts from the end of, and the caller has already ranked it.
    """
    ceiling = min(RECENT_CONTEXT_MAX_CHARS, limit // 2)
    entries: list[dict[str, Any]] = []
    used = 0
    for entry in recent_context:
        if len(entries) >= RECENT_CONTEXT_MAX_ENTRIES:
            break
        cost = len(str(entry.get("title") or "")) + len(str(entry.get("statement") or ""))
        if used + cost > ceiling:
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


#: Statuses that RETIRE a page: the tree's OWN inactive vocabulary, less
#: the two that mean pre-active rather than retired.
#:
#: Derived rather than written out, because a hand-written list was wrong in
#: both directions — it invented `retired` and `deprecated`, which name no
#: page status anywhere else here, and it missed `dropped`, so a page the
#: author dropped was carried and its unit served as current memory.
#: `draft` and `planned` are carved out deliberately: both mean authored and
#: not yet active, which is a page a turn naming it wants, not one the vault
#: has stopped standing behind.
def _retired_page_statuses() -> frozenset[str]:
    from . import activation

    return frozenset(activation._INACTIVE_STATUSES) - {"draft", "planned"}


RETIRED_PAGE_STATUSES: frozenset[str] = _retired_page_statuses()


def _is_current_page(vault_root: Path, rel_path: str) -> bool:
    """Is `rel_path` a page the vault still stands behind?

    A page the author retired — a `RETIRED_PAGE_STATUSES` status, or a
    `superseded_by` pointing at its replacement — is not a page to answer a
    turn from. `draft` and `planned` are NOT retirement: both mean authored
    and not yet active, and a turn that names such a page wants it.

    Lifecycle is decided here rather than counted later because a retired
    page and its replacement are named by the SAME words: "the girvan slot
    window" names both the current note and the one it superseded. Counting
    them as two named pages would refuse every revised page in the vault,
    and serving the loser would hand back the stale figure, which is the
    worse of the two.

    Reads the same facts the unit lane already reads for supersession, from
    `find_corpus.CACHE` — the request path's own cached single-page read, no
    walk, and at most `RETRIEVAL_CARRY_LIMIT` of them. A page that cannot be
    read is not proven current, so it is not a candidate.
    """
    text = str(rel_path or "")
    if not text.endswith(".md"):
        return False
    try:
        from . import find_corpus

        root = Path(vault_root)
        page = find_corpus.CACHE.get(root / text, root)
    except Exception:  # noqa: BLE001 - an unreadable page is simply not a candidate
        log.debug("activation carry lifecycle read failed for %s", text, exc_info=True)
        return False
    if page is None:
        return False
    if getattr(page, "superseded_by", None):
        return False
    frontmatter = page.frontmatter if isinstance(page.frontmatter, Mapping) else {}
    status = working_set_index.normalize(frontmatter.get("status") or "active")
    return status not in RETIRED_PAGE_STATUSES


def _canonical_agent_page_ref(vault_root: Path, ref: str) -> str | None:
    """`ref`, unchanged, if it is ALREADY the catalogue's own canonical
    spelling of a page it knows — else `None`.

    Checked before anything reads a file, in this order, cheapest first:

    1. Not empty, no backslash anywhere (a POSIX path separator is `/` only;
       a backslash is an ordinary filename character to `Path`, and folding
       it — the way `working_set_runtime._is_raw_material`/
       `_is_navigation_page` fold it for their OWN string comparisons — was
       never applied to the read that follows, so a backslashed spelling of
       an ordinary page reached the file it named anyway).
    2. Not absolute (`posixpath.isabs`) and not a spelling
       `posixpath.normpath` would write differently — one rule that covers a
       leading `./`, a doubled slash, a trailing slash, and a literal `.`/
       `..` segment together, rather than one pattern for each.
    3. Under the knowledge-base folder (`kbdir.kb_prefix()`).
    4. A row the LEXICAL catalogue already holds under this EXACT string —
       one indexed lookup (`lexstore.page_content_hashes`), never a search
       and never a directory read.

    Every other spelling of an eligible page — `./Knowledge Base/...`, a
    doubled slash, a backslashed separator, an absolute path, a `..` that
    lands back inside the knowledge base — used to reach `_is_current_page`
    (which reads the file) and, for a page that turned out eligible, all the
    way to `_carried_packet`'s full unit lane: several times an unknown
    ref's cost, measured, and a timing side channel that told an audience
    apart from a page's own canonical name without ever naming it back.
    Refused here instead, at the same cost as an unknown ref, before any
    read — never a distinguishable answer from the ordinary unknown-ref
    refusal `_eligible_agent_page` already gives.

    A catalogue that cannot answer (unbuilt, mid-repair, FTS5 unavailable)
    answers `None` for every ref, exactly as the retrieval carry itself
    already declines its OWN candidates when the SAME catalogue cannot prove
    itself current — no separate failure mode, no separate policy.
    """
    text = str(ref or "").strip()
    if not text or "\\" in text or posixpath.isabs(text):
        return None
    if posixpath.normpath(text) != text:
        return None
    from .kbdir import kb_prefix

    if not text.startswith(kb_prefix()):
        return None
    from . import lexstore

    hashes = lexstore.page_content_hashes(vault_root, [text])
    if hashes.get(text) is None:
        return None
    return text


def _eligible_agent_page(vault_root: Path, ref: str) -> str | None:
    """The vault-relative page `ref` names, if `anchor` may carry a packet from
    it, else `None`.

    An anchor override tries the activation index's own rows first
    (`override_candidates`); this is the fallback for a ref that named no row
    there. `_canonical_agent_page_ref` runs FIRST and reads nothing: only a
    ref already spelled the way the catalogue stores it, and already proven
    a row that catalogue holds, reaches the checks below at all. Those reuse
    the retrieval carry's OWN eligibility test rather than a second opinion
    about what a servable page is — the same three refusals a turn that
    merely NAMED a page already gets: not raw material
    (`working_set_runtime._is_raw_material`, `Sources/`/`Evidence/`), not
    navigation (`working_set_runtime._is_navigation_page`,
    `find_corpus.NAVIGATION_BASENAMES`), and current — existing, Markdown,
    not retired (`_is_current_page`, above).
    """
    path = _canonical_agent_page_ref(vault_root, ref)
    if path is None:
        return None
    from . import working_set_runtime

    if working_set_runtime._is_raw_material(path):
        return None
    if working_set_runtime._is_navigation_page(path):
        return None
    if not _is_current_page(vault_root, path):
        return None
    return path


def rare_document_cap(corpus_pages: int) -> int:
    """The document frequency at or below which a stem counts as DISTINCTIVE
    in a corpus of `corpus_pages` indexed pages.

    `max(RETRIEVAL_CARRY_RARE_MIN_DOCS, ceil(share * pages))`. Corpus-relative
    because that is the only way the judgement survives a growing vault: a
    fixed cap is the same mistake a fixed score floor was.
    """
    pages = max(0, int(corpus_pages))
    return max(RETRIEVAL_CARRY_RARE_MIN_DOCS, -(-pages * 5 // 1000))


def carry_fetch_size(corpus_pages: int) -> int:
    """How many ranked rows to read before filtering, for a corpus of
    `corpus_pages` pages.

    Never fewer than `RETRIEVAL_CARRY_FETCH`, and always MORE than the
    number of pages the rarity gate can admit. Up to `rare_document_cap`
    pages may share one distinctive phrase, and that cap passes a fixed ten
    from 2,001 pages — 11 at 2,200, 25 at 5,000, 50 at 10,000. Above that a
    fixed window can be filled entirely by retired pages sharing the phrase,
    leaving one current page inside it; the count then reads one and a turn
    that named a second page carries the first anyway. That is the failure
    the count exists to prevent, arriving through the limit instead of
    through the score.

    One more than the cap, so a window full of excluded rows still leaves
    room for the row that proves there were two.
    """
    return max(RETRIEVAL_CARRY_FETCH, rare_document_cap(corpus_pages) + 1)


def dominant_carry(hits: Sequence[tuple[str, float]]) -> tuple[str, float] | None:
    """The one page `hits` says the turn named, or `None`.

    Every entry has already passed the naming gate — a distinctive phrase —
    and been filtered to pages that are current and not raw material. So
    `hits` IS the set of pages this turn named, and the question here is
    only how many there are.

    Exactly one is a packet. Two or more is a turn that named two things,
    and choosing between them is the guess the compiler exists not to make:
    the score gap carries no information about which was meant, since two
    equally-named pages measured 19.60 against 19.16 on one turn and 31.07
    against 17.40 on another differing only in wording. The turn abstains
    and the client, which can see it abstained, is free to ask.

    `RETRIEVAL_CARRY_MIN_SCORE` is the one absolute left, and it only
    refuses a row the ranking placed at nothing.
    """
    if len(hits) != 1:
        return None
    path, score = hits[0]
    if score <= RETRIEVAL_CARRY_MIN_SCORE:
        return None
    return str(path), float(score)


def _carry_by_retrieval(
    vault_root: Path,
    *,
    turn: str,
    timings: Any = None,
    freshness_snapshot: Any = None,
    lexical_seconds: float = 0.0,
) -> tuple[tuple[str, float], ...]:
    """The pages this turn NAMED, scored, current, and not raw material.

    Empty means the turn named nothing and abstains exactly as it did. One
    is a packet. Two or more is a turn that named several things: the
    caller abstains and lists them, so the client can ask for one by name
    rather than being handed an empty packet.

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
        return ()
    freshness = None
    recall_checkpoint = None
    if freshness_snapshot is not None:
        try:
            freshness = freshness_snapshot.for_scope("kb")
            recall_checkpoint = freshness_snapshot.recall_checkpoint("kb")
        except Exception:  # noqa: BLE001 - an unreadable snapshot carries nothing
            log.debug("activation carry freshness unavailable", exc_info=True)
            return ()
    with _span(timings, "working_set.carry"):
        from . import working_set_runtime

        hits, state = working_set_runtime.carry_candidates(
            vault_root,
            turn,
            freshness=freshness,
            recall_checkpoint=recall_checkpoint,
        )
    if state != "available":
        return ()
    return hits[: working_set_resolve.MAX_ANCHORS]


def _page_lifecycle(vault_root: Path, rel_path: str) -> str:
    """A page's own normalised status, or `"active"` when it declares none.

    Reported rather than assumed. A carried or named page can legitimately
    be a `draft` or `planned` — both are candidates, being authored and not
    yet active — and a packet that called every one of them `active` would
    be telling the reader something the page does not say. Reads the cache
    the lifecycle check has already warmed for exactly these paths.
    """
    text = str(rel_path or "")
    if not text.endswith(".md"):
        return "active"
    try:
        from . import find_corpus

        root = Path(vault_root)
        page = find_corpus.CACHE.get(root / text, root)
    except Exception:  # noqa: BLE001 - a lifecycle label is never worth a failure
        log.debug("activation page lifecycle read failed for %s", text, exc_info=True)
        return "active"
    if page is None:
        return "active"
    frontmatter = page.frontmatter if isinstance(page.frontmatter, Mapping) else {}
    return working_set_index.normalize(frontmatter.get("status") or "active") or "active"


def _page_title(vault_root: Path, rel_path: str) -> str:
    """A page's own authored title, or `""`.

    Reads `find_corpus.CACHE`, which the lifecycle check has already warmed
    for exactly these paths, so this is a cache hit rather than a second
    read. A page that is not an anchor has no title in the catalogue, and a
    menu of filenames is a worse menu than a menu of titles.
    """
    text = str(rel_path or "")
    if not text.endswith(".md"):
        return ""
    try:
        from . import find_corpus

        root = Path(vault_root)
        page = find_corpus.CACHE.get(root / text, root)
    except Exception:  # noqa: BLE001 - a title is a courtesy, never a promise
        log.debug("activation named-page title read failed for %s", text, exc_info=True)
        return ""
    if page is None:
        return ""
    frontmatter = page.frontmatter if isinstance(page.frontmatter, Mapping) else {}
    return str(frontmatter.get("title") or getattr(page, "title", "") or "").strip()


def _named_anchors(
    vault_root: Path,
    named: Sequence[tuple[str, float]],
    *,
    index: working_set_index.WorkingSetIndex | None = None,
) -> tuple[dict[str, Any], ...]:
    """The pages a turn named, as anchor entries, for an abstention that has
    nothing to carry.

    Same shape a carried page gets — `kind: "page"`, `retrieval` and nothing
    else as evidence — at `retrieval_named`, which says the turn's words
    reached this page and no packet was built from it. Each one crosses the
    egress guard as an ordinary anchor, so a page this audience may not see
    is removed from the list like any other.
    """
    return tuple(
        {
            "ref": path,
            "path": path,
            "title": _page_title(vault_root, path) or _indexed_title(index, path) or path,
            "kind": "page",
            "lifecycle": _page_lifecycle(vault_root, path),
            "status": working_set_resolve.RETRIEVAL_NAMED_STATUS,
            "evidence": ["retrieval"],
        }
        for path, _score in named
    )


def _indexed_title(index: working_set_index.WorkingSetIndex | None, path: str) -> str:
    """The authored title the anchor catalogue already holds for `path`, or
    `""`. Reads rows the request has in hand; never a file, never a walk.

    A lookup by path rather than a scan compared against it: the rows are
    read once into a mapping and asked once. The catalogue is bounded, so
    the scan was never slow — it was a scan written where a lookup belongs,
    and the shape is what makes it obvious that one carried page costs one
    question.
    """
    if index is None or not path:
        return ""
    try:
        titles = {
            str(getattr(row, "path", "") or ""): str(getattr(row, "title", "") or "")
            for row in index.anchors()
        }
    except Exception:  # noqa: BLE001 - a title is a courtesy, never a promise
        log.debug("activation carry title lookup failed", exc_info=True)
        return ""
    return titles.get(path, "")


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
    index: working_set_index.WorkingSetIndex | None = None,
    recent_context: Sequence[Mapping[str, Any]] = (),
    status: str = working_set_resolve.RETRIEVAL_CARRIED_STATUS,
    evidence: tuple[str, ...] = ("retrieval",),
    carried_by: str = "retrieval",
) -> dict[str, Any] | None:
    """One packet compiled from a single dominant page, marked as carried.

    `status`/`evidence`/`carried_by` default to the retrieval carry's own
    spelling (design D3): the page is reported as ONE anchor entry of kind
    `page` at status `retrieval_carried`, whose only evidence is `retrieval`.
    That spelling is the whole honesty of the feature: a reader can tell at a
    glance that no anchor was named and that recall alone put this material
    here. `mint_continuity` names the carried page's path in the token, so
    "continue" can resume it: the hot profile ranks pages as well as anchors.

    An agent-picked page (`anchor` naming an ordinary compiled page that is
    not an index anchor) reuses this SAME builder and the same units lane,
    at `status="resolved"`, `evidence=("agent_choice",)`, `carried_by=
    "agent_choice"` instead — the identical soundness-rule outcome an anchor
    override already gets when the ref DOES match an index row
    (`DECIDING_ALONE_KINDS`). Nothing downstream (`mint_continuity`, the
    egress guard, the hook) has to learn a second meaning of "the agent chose
    this": it is the same meaning, reached on a page instead of an anchor.

    Title and lifecycle are taken from the units the lane actually read off
    that page, never asserted: the unit rows already carry their parent
    page's own title and supersession, so the anchor entry says what the
    page says about itself. A page that is ALSO an anchor row the turn
    failed to resolve has its authored title in the index already, which is
    the next place to look. The path is the last fallback, not the first
    answer.

    `None` when the lanes read nothing off the page. A page can dominate
    recall and still have nothing a unit role selects — its units carry
    other categories, or it has none — and serving that as a packet with
    `abstained: false` and an empty `units` block states that the turn
    resolved and the vault had nothing, which is a different and false
    claim. The caller abstains `unresolved` instead, which is what the turn
    did before the carry existed and what the hook can still render a menu
    for.

    The carried page is the packet's ONLY anchor, and that is load-bearing
    rather than incidental. An `unresolved` abstention lists the turn's
    `partial` candidates in `anchors[]` so the agent can choose one; carrying
    them here as well would break the egress rule that makes a withheld
    carried page safe — `guard_working_set` turns a packet into a `withheld`
    abstention only when EVERY anchor was withheld, so surviving partials
    would leave the packet claiming it resolved something after the one page
    it was built from was removed. The cost is that a carried turn no longer
    shows that menu; the material it shows instead is the trade.

    `recent_context` is passed straight through to `build_packet`, so a
    carried packet leads with working continuity exactly as a resolved or an
    abstained one does. It is not material about the carried page and the
    carry does not decide it: the caller assembled it before resolution was
    even branched on, and every exit from `compile_packet` carries the same
    block.
    """
    path, _score = page
    roles = _carry_roles(registry, analysis)
    carried = working_set_resolve.ResolvedAnchor(
        anchor_id=path,
        path=path,
        ref=None,
        title=_indexed_title(index, path) or path,
        kind="page",
        lifecycle=_page_lifecycle(vault_root, path),
        status=status,
        evidence=evidence,
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
    if not items:
        return None
    for item in items:
        if item.path == path:
            # The lane's own reading first, the index's second, the path
            # last: a lane that knew no title must not overwrite one the
            # catalogue already holds. The LIFECYCLE is not taken from the
            # lane at all — a lane item's lifecycle describes the UNIT, and
            # the page's own status is already on the anchor, so letting it
            # through here would report a draft page as active.
            carried = replace(carried, title=item.title or carried.title or path)
            break

    generation = {**generation, "carried_by": carried_by}
    if budget_exhausted("working_set.budget"):
        raise BudgetExhausted("working_set.budget")
    with _span(timings, "working_set.budget"):
        # The literal `"resolved"` below is `build_packet`'s own "did this
        # turn produce material" flag — the one thing it decides `abstained`
        # from. It is NOT this function's `status` parameter: `resolution`
        # (the caller's) stayed `unresolved` in both carry cases, which is
        # why this packet exists at all. What the turn actually did is in
        # the anchor's own `status` (`retrieval_carried`, or `resolved` for
        # an agent-picked page) and in `generation.carried_by`.
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
            recent_context=recent_context,
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
    continuity_minted_ns: int | None = None,
    continuity_passed: bool | None = None,
    anchor: str | None = None,
    lexical_seconds: float = 0.0,
    heat_profile: working_set_heat.HeatProfile | None = None,
    attribution: working_set_heat.Attribution | None = None,
    marks: Mapping[str, working_set_heat.SessionMark] | None = None,
) -> dict[str, Any]:
    """Resolve, select, retrieve and budget — the whole compiler in one call.

    `anchor` is the agent's own choice of sense: it replaces resolution outright
    rather than joining it, because the agent is the decider of an ambiguous turn
    and the competing senses are then not candidates at all. `continuity_refs`
    qualifies anchors this turn already reached; on a referential turn that
    names nothing it is also the first tier of the hot profile, and may supply
    the referent that turn points at (design §8).

    `heat_profile` is the projection the caller keyed its cache on (`serve`),
    so a packet is always compiled against the profile it was keyed on; left
    out, it is read here. `attribution` is the caller's derived session and
    workspace keys (ruling S5-1) and `marks` the served threads the caller may
    see, both for the ranking only.
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
    # Bounded work between two boundaries that already gate the request, for
    # the reason `working_set.recent` takes no boundary of its own (below).
    heat = heat_profile
    if heat is None:
        with _span(timings, "working_set.heat"):
            heat = working_set_heat.profile(root)
    # "Missing or stale profiles SHALL report their state": `current` (live
    # watcher, complete delta), `partial` (no watcher: governed activity only),
    # `seeded`, `behind` (a reconcile is pending) or `empty`. The session start
    # is a day, the granularity the recent-context block already dates at.
    generation["hot_profile"] = {
        "state": heat.state,
        "session_start": _recent_as_of(heat.session_start_ns),
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
        # Copied once per request for the recent-context block after
        # resolution. The hot profile reads the heat projection instead.
        mtimes = _recent_mtimes(root)
        # Whether a valid token was passed at all, which is not the same as
        # whether any of its refs survived: the caller drops every ref the
        # audience may not see (`working_set_runtime.visible_continuity_refs`),
        # and a token whose refs all went still leads the hot profile, exactly
        # as one whose refs name nothing does.
        passed = bool(continuity_refs) if continuity_passed is None else bool(continuity_passed)
        hot = HotSet()
        if anchor:
            chosen = working_set_resolve.override_candidates(rows, anchor)
            # A ref that names no anchor is not a packet with nothing in it: the
            # caller asked about a sense that does not exist here. It abstains,
            # and `op_activate_context` turns that into the one refusal an
            # unknown and a withheld ref share.
            resolution = working_set_resolve.resolve(chosen, turn_tokens=analysis.tokens)
        else:
            # Computed once per request, like `used_paths`, and for the same
            # reason: it is a fact about the vault that every row is measured
            # against, not something the loop can derive. Only for a
            # referential turn: on any other the prior decides nothing, so it
            # is not computed and the turn resolves exactly as it did before
            # the prior could supply a referent.
            hot = (
                hot_profile(
                    root,
                    rows=rows,
                    continuity_refs=continuity_refs,
                    continuity_minted_ns=continuity_minted_ns,
                    continuity_passed=passed,
                    profile=heat,
                    attribution=attribution,
                    marks=marks,
                )
                if analysis.referential
                else HotSet()
            )
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
                # Anchor members only, and none at all when the leading tier
                # holds a page: a page is served below, never through the
                # resolver, and a mixed tier is reported, never guessed.
                hot_paths=hot.anchor_paths if not hot.pages else frozenset(),
                term_anchor_counts=index.term_anchor_counts(),
            )
            candidates = working_set_resolve.add_graph_corroboration(
                candidates, retrieval_paths=retrieval_paths
            )
            candidates = working_set_resolve.apply_continuity(candidates, continuity_refs)
            resolution = working_set_resolve.resolve(
                candidates,
                turn_tokens=analysis.tokens,
                referential=analysis.referential,
            )
        if passed:
            # `applied` only when a ref qualified something: an anchor this
            # resolution carries on `continuity` here, or the page resumed
            # below. A ref that merely names a row the turn never reached
            # contributed nothing. The caller reports this in place of its own
            # `applied`.
            generation["continuity"] = (
                "applied"
                if any("continuity" in item.evidence for item in resolution.anchors)
                else "stale"
            )

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
        recent: tuple[dict[str, Any], ...] = _recent_context(root, rows=rows, mtimes=mtimes)

    # Design D3, and ONLY here: the turn reached no anchor at all. An
    # `ambiguous` turn is untouched (it reached two, and picking between them
    # is the agent's job), an `agent_choice` turn is untouched (a ref that
    # named nothing must keep abstaining `unresolved`, which is what
    # `op_activate_context` turns into its one refusal), and a turn that
    # resolved anything never reaches this line. Carrying is a PACKET-level
    # decision taken after resolution has already abstained — it adds no
    # evidence kind, changes no status rule, and can never resolve an anchor.
    # A referential turn never reaches it either (close-memory-loop D2): it
    # says nothing besides its cue and filler words, so it names no page for
    # the carry to find, and the carry is not asked.
    # A referential turn whose leading tier holds an ordinary compiled page
    # (design §2.7, batch review a2): a page the agent picked, recall carried,
    # or the user worked on that is not an anchor row. Only when the turn
    # resolved nothing and no candidate carries worded contact — the fifth
    # clause's own condition — so a named anchor always wins. One page is
    # served through the same builder the pick used; a page tied with
    # anything else is reported, never guessed.
    if (
        not anchor
        and analysis.referential
        and resolution.status == "unresolved"
        and hot.pages
        and not any(
            set(item.evidence) & working_set_resolve.WORDED_CONTACT_KINDS
            for item in resolution.anchors
        )
    ):
        if len(hot.members) == 1:
            (page,) = hot.pages
            # U7's spelling when the caller's own token supplied the page;
            # recency alone otherwise.
            packet = _carried_packet(
                root,
                page=(page, 0.0),
                analysis=analysis,
                registry=registry,
                limit=limit,
                purpose=purpose,
                timings=timings,
                generation=(
                    {**generation, "continuity": "applied"} if hot.from_token else generation
                ),
                index_token=index_token,
                freshness_snapshot=freshness_snapshot,
                index=index,
                recent_context=recent,
                status="resolved",
                evidence=("continuity", "recency") if hot.from_token else ("recency",),
                carried_by="continuity" if hot.from_token else "recency",
            )
            if packet is not None:
                return packet
        else:
            return abstained_packet(
                reason="ambiguous",
                max_chars=limit,
                generation=generation,
                anchors=(),
                ambiguity=_hot_ambiguity(root, hot, rows),
                recent_context=recent,
            )

    if not anchor and resolution.status == "unresolved" and not analysis.referential:
        named = _carry_by_retrieval(
            root,
            turn=turn,
            timings=timings,
            freshness_snapshot=freshness_snapshot,
            lexical_seconds=lexical_seconds,
        )
        carried = dominant_carry(named)
        if carried is None and named:
            # The turn named several pages. Nothing is carried, but an
            # abstention that says nothing at all leaves the client with an
            # empty packet and no way to know a question would help. The
            # named pages are listed at `retrieval_named` so it can ask for
            # one; the reason stays `unresolved`, because nothing resolved.
            return abstained_packet(
                reason=resolution.status,
                max_chars=limit,
                generation=generation,
                anchors=_named_anchors(root, named, index=index),
                ambiguity=resolution.ambiguity,
                recent_context=recent,
            )
        if carried is not None:
            # `None` back means the lanes read nothing off that page, so it
            # falls through to the ordinary `unresolved` abstention below —
            # with the resolution's OWN anchors, the partial candidates the
            # hook renders as a menu, because this turn ended up exactly
            # where it would have without the carry.
            packet = _carried_packet(
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
                index=index,
                recent_context=recent,
            )
            if packet is not None:
                return packet

    # The agent-pick fallback: `anchor` named no row in the activation index
    # (`override_candidates` above found nothing, so `resolution.status`
    # stayed `unresolved` — the only other value an override branch can
    # reach), but the ref may still name an ordinary compiled page this
    # audience can see. Reuses `_carried_packet` unchanged, at the
    # soundness rule's OWN "agent decides alone" outcome
    # (`working_set_resolve.DECIDING_ALONE_KINDS`) rather than the carry's
    # `retrieval_carried` one: the agent named this page, recall did not
    # merely surface it.
    if anchor and resolution.status == "unresolved":
        agent_page = _eligible_agent_page(root, anchor)
        if agent_page is not None:
            packet = _carried_packet(
                root,
                page=(agent_page, 0.0),
                analysis=analysis,
                registry=registry,
                limit=limit,
                purpose=purpose,
                timings=timings,
                generation=generation,
                index_token=index_token,
                freshness_snapshot=freshness_snapshot,
                index=index,
                recent_context=recent,
                status="resolved",
                evidence=("agent_choice",),
                carried_by="agent_choice",
            )
            if packet is not None:
                return packet
            # `None` back means the lanes read nothing off that page (it
            # exists and is eligible, but carries no unit a role selects).
            # Falls through to the ordinary `unresolved` abstention below,
            # which `op_activate_context` turns into the one refusal an
            # unknown ref and a withheld one share — the same outcome a
            # retrieval carry that read nothing off its page reaches.

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


class HotSet(NamedTuple):
    """The referent a turn that names nothing is taken to point at.

    `members` is the leading tier, cut at `HOT_PROFILE_K`; `anchor_paths` are
    the members that are activation-index rows and `pages` the ordinary
    compiled pages among them. `tier` is ruling S5-1's (1 own session, 2 same
    workspace, 3 vault) and `from_token` says the caller's passed continuity
    token supplied them, which is what lets a page referent say so.
    """

    members: frozenset[str] = frozenset()
    anchor_paths: frozenset[str] = frozenset()
    pages: frozenset[str] = frozenset()
    tier: int = working_set_heat.TIER_VAULT
    from_token: bool = False


def hot_profile(
    vault_root: Path,
    *,
    rows: Sequence[Any],
    continuity_refs: frozenset[str] = frozenset(),
    continuity_minted_ns: int | None = None,
    limit: int = HOT_PROFILE_K,
    continuity_passed: bool | None = None,
    profile: working_set_heat.HeatProfile | None = None,
    attribution: working_set_heat.Attribution | None = None,
    marks: Mapping[str, working_set_heat.SessionMark] | None = None,
) -> HotSet:
    """The TOP of the heat projection's ranking — what a turn that names
    nothing is taken to be referring to (design §8, close-memory-loop 7.3).

    The ranking is `working_set_heat.leading`'s, stated there and nowhere
    else: the caller's own session first, then its workspace, then the vault
    (ruling S5-1); inside a tier a continuity thread leads, taken whole, until
    a deliberate act elsewhere is newer than it, and otherwise rows rank by
    their latest deliberate act and then their latest selection inside the
    latest working session. Heat comes from events the projection recorded
    — governed work by origin, admitted picks, episodes, released reads —
    never from a file's current mtime, so a maintenance batch cannot erase
    what the user was doing.

    The profile is the LEADING TIER only, never the top `limit`: a turn that
    names nothing refers to one thing. Ties are sorted by path and CUT at
    `limit`, a bound on how wide a menu may be, never a choice.

    Members may be anchor rows or ordinary compiled pages (U7's picks and
    carries are pages). Retired state is never offered: an anchor row's own
    indexed lifecycle and the recent-context block's working-context rule are
    free string checks, and each member the walk reaches is checked for
    currency — `_is_current_page` for a row, `_eligible_agent_page` for a
    page — at most `limit` cached single-page reads, as before.
    """
    root = Path(vault_root)
    heat = profile if profile is not None else working_set_heat.profile(root)
    by_path = {
        str(getattr(row, "path", "") or ""): row for row in rows if getattr(row, "path", "")
    }
    collections = _recent_collection_dirs((*by_path, *heat.all_rows))
    retired = {
        path
        for path, row in by_path.items()
        if str(getattr(row, "lifecycle", "active") or "active") in RETIRED_PAGE_STATUSES
    }

    def admissible(path: str) -> bool:
        return path not in retired and _recent_reason_for(path, collections=collections) == "edited"

    passed = bool(continuity_refs) if continuity_passed is None else bool(continuity_passed)
    token_paths = frozenset(
        path
        for path in (
            *(
                path
                for path, row in by_path.items()
                if working_set_resolve.names_row(continuity_refs, row)
            ),
            *(
                ref
                for ref in continuity_refs
                if ref.endswith(".md")
                and not any(
                    working_set_resolve.names_row(frozenset({ref}), row) for row in by_path.values()
                )
            ),
        )
        if admissible(path)
    )
    leads = working_set_heat.leading(
        heat,
        attribution=attribution,
        token_paths=token_paths,
        token_minted_ns=continuity_minted_ns,
        token_passed=passed,
        admissible=admissible,
        marks=marks,
    )

    def eligible(path: str) -> bool:
        if path in by_path:
            return _is_current_page(root, path)
        return _eligible_agent_page(root, path) is not None

    chosen = working_set_heat.members(leads, limit=limit, eligible=eligible)
    members = frozenset(chosen.paths)
    anchors = frozenset(path for path in members if path in by_path)
    from_token = bool(chosen.token and passed and members and members <= token_paths)
    return HotSet(members, anchors, members - anchors, chosen.tier, from_token)


def _hot_ambiguity(
    vault_root: Path, hot: HotSet, rows: Sequence[Any]
) -> tuple[dict[str, Any], ...]:
    """A leading tier that mixes a page with anything else, reported as a
    choice (design D7): the server never guesses between a page and another
    member. Pages are listed as `kind: "page"` with no neighbourhood."""
    by_path = {str(getattr(row, "path", "") or ""): row for row in rows}
    entries: list[dict[str, Any]] = []
    for path in sorted(hot.members):
        row = by_path.get(path)
        if row is None:
            entries.append(
                {
                    "ref": path,
                    "title": _page_title(vault_root, path) or Path(path).stem,
                    "kind": "page",
                    "neighbourhood_size": 0,
                }
            )
            continue
        entries.append(
            {
                "ref": working_set_resolve.anchor_ref(row),
                "title": str(getattr(row, "title", "") or "") or Path(path).stem,
                "kind": str(getattr(row, "kind", "") or ""),
                "neighbourhood_size": len(getattr(row, "neighbourhood", ()) or ()),
            }
        )
    return tuple(entries)


def _counts_toward_burst(path: str) -> bool:
    """`working_set_heat.counts_toward_burst`, kept under its old name."""
    return working_set_heat.counts_toward_burst(path)


def _burst_paths(edited: Mapping[str, int]) -> frozenset[str]:
    """`working_set_heat.burst_paths`: the rule now lives beside the fold and
    the seed, the only two places that still judge a change by its timing."""
    return working_set_heat.burst_paths(edited)


def _activation_snapshot() -> Mapping[str, float]:
    """The memoized ACT-R activation map, or an empty one.

    Optional by construction, like every other source the working-continuity
    block reads: a vault with no usage log is not a vault with no recent work.
    """
    try:
        from . import ranking_config, usage

        return usage.activation_map(ranking_config.DEFAULT_RANKING) or {}
    except Exception:  # noqa: BLE001 - the usage snapshot is optional by construction
        log.debug("recent context: usage activation unavailable", exc_info=True)
        return {}


def _recent_context(
    vault_root: Path,
    *,
    rows: Sequence[Any],
    limit: int = RECENT_CONTEXT_MAX_ENTRIES,
    mtimes: Mapping[str, int] | None = None,
) -> tuple[dict[str, Any], ...]:
    """What was recently worked on — the block a turn that resolved nothing
    still carries.

    Four sources, none of which enumerates a directory or walks the vault:

    * the freshness registry's per-path mtimes, which the watcher maintains and
      this reads as a dict (`edited`, `captured` for a captured session, or
      `episode` for a conversation's newest recap, one entry per episode);
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
    `limit`. A page offered for its reads has no recent edit to rank by, so
    it ranks after the timed entries, by its reads. The edit, read and
    retirement rules are the hot profile's own (`hot_profile`). `as_of` dates the CONTACT, never the event the page describes: a
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
    mtimes = _recent_mtimes(root) if mtimes is None else mtimes
    # From the index's rows AS WELL AS the freshness map. Without a watcher the
    # map is empty, but the other two sources still run off the rows — so
    # deriving the collection directories from the map alone left the exclusion
    # inert on exactly the cold path where those sources are all there is.
    collections = _recent_collection_dirs((*by_path, *mtimes))
    offered = _recent_edits(mtimes, limit=limit, collections=collections)
    # Read pages rank by how much they were read, not by their last edit:
    # an edit time says nothing about a read, and ranking by it let any
    # eight fresher edits cut every read page from the block.
    activated = {
        rel: order
        for order, rel in enumerate(
            _recently_activated(by_path, mtimes, limit=limit, collections=collections)
        )
    }
    for rel in activated:
        offered.setdefault(rel, "activated")
    for rel in _recent_planning(by_path, mtimes, limit=limit, collections=collections):
        offered.setdefault(rel, "planning")
    # An episode is offered once per conversation, its newest revision, never
    # per file, and like a captured session it is exempt from the burst
    # cutoff: it is the record of what was spoken about, not a batch edit.
    for rel in _recent_episodes(mtimes, limit=RECENT_EPISODES_MAX):
        offered[rel] = "episode"
    # Retired state is never offered, as the hot profile never offers it: a
    # retired status or a `superseded_by` pointer, read from the request's
    # own page cache for the offered pages only (at most three sources of
    # `limit` each, and the episodes), which the statement below reads next
    # anyway. A captured session is raw material and has no lifecycle to read.
    offered = {
        rel: why
        for rel, why in offered.items()
        if why == "captured" or _is_current_page(root, rel)
    }

    def _rank(item: tuple[str, str]) -> tuple[int, int, int, str]:
        path, why = item
        if why == "activated":
            return (0, RECENT_CONTEXT_REASONS.index(why), activated.get(path, 0), path)
        return (-mtimes.get(path, 0), RECENT_CONTEXT_REASONS.index(why), 0, path)

    # One slot is RESERVED for the newest open Planning item. Ranking the
    # whole block by recency buried it every time: an open commitment nobody
    # has touched is by definition older than the edits, so eight fresh pages
    # cut it, `_recent_planning` could never put anything in the block, and
    # the commitment a resumed session most needs reminding of was the one
    # thing guaranteed missing. It is a reservation, not a takeover — the
    # remaining slots stay recency-ranked, and the entry keeps its place in
    # that order rather than being pinned to the front.
    #
    # The newest episode is reserved a slot for the same reason: once work
    # has resumed, the last conversation is older than the edits it led to,
    # and it is what a resumed session most needs. The most-read page is
    # reserved one on the same terms: a read page has no fresh edit by
    # definition (a fresh edit would have offered it as `edited`), so eight
    # fresh edits cut it every time.
    reserved = [
        min(group, key=_rank)
        for group in (
            [(path, why) for path, why in offered.items() if why == reason]
            for reason in _RESERVED_RECENT_REASONS
        )
        if group
    ][: max(0, limit)]
    # Reserve the slots, then backfill from EVERYTHING left, the offers that
    # did not get a slot included, episodes up to their cap. Reserving
    # without backfilling left the block short on the vault it exists for —
    # two recent edits and five open plans filled three of eight slots, and
    # four open commitments were never offered at all.
    episodes = sum(1 for _path, why in reserved if why == "episode")
    backfill: list[tuple[str, str]] = []
    for item in sorted((item for item in offered.items() if item not in reserved), key=_rank):
        if len(backfill) >= limit - len(reserved):
            break
        if item[1] == "episode":
            if episodes >= RECENT_EPISODES_MAX:
                continue
            episodes += 1
        backfill.append(item)
    ranked = sorted([*backfill, *reserved], key=_rank)

    entries: list[dict[str, Any]] = []
    for path, why in ranked:
        row = by_path.get(path)
        kind = "episode" if why == "episode" else str(getattr(row, "kind", "") or "") or "page"
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
        if entry["why"] == "episode":
            # The recording agent's own subject and summary: authored, like
            # any page's `summary`, never a sentence the server wrote. The
            # same one cached read every other entry gets.
            entry.update(_recent_episode_fields(root, entry["path"]))
            continue
        statement = _recent_frontmatter_statement(root, entry["path"])
        if statement:
            entry["statement"] = statement
    return _without_collection_echoes(entries, collections)


def _recent_edits(
    mtimes: Mapping[str, int],
    *,
    limit: int,
    collections: frozenset[str] = frozenset(),
) -> dict[str, str]:
    """`{path: why}` for the newest `limit` pages that are working context
    (`edited`, or `captured` for a captured session), newest first, less any
    edit at or before the latest write burst. A captured session is never cut.

    That is the hot profile's own edit rule (`hot_profile`): a last edit
    inside a write burst is a batch nobody chose, and one older than the
    latest burst may have lost the user's own page to it. An edit after the
    burst is work again.

    Only the LATEST burst matters, and only whether it reaches back over the
    offers, so neither the registry nor every burst is sorted: a heap hands
    the entries over newest first, the first chain that reaches
    `HOT_PROFILE_BURST_PAGES` is the latest burst (`_burst_paths`' own chain
    rule, read from the other end), and the scan stops once the offers are
    full and no chain still open could reach back over them. The answer is
    the one the full sort gives.
    """
    import heapq

    if limit <= 0:
        return {}
    heap = [(-int(mtime), rel) for rel, mtime in mtimes.items()]
    heapq.heapify(heap)
    offers: list[tuple[int, str, str]] = []
    after_burst: int | None = None
    # The open chain: its newest edit, its oldest so far, and its length.
    top = last = size = 0
    while heap:
        negative, rel = heapq.heappop(heap)
        mtime = -negative
        if len(offers) >= limit:
            if after_burst is not None:
                break
            oldest = offers[-1][0] if offers else 0
            if mtime < oldest and (not size or top < oldest):
                break
        else:
            why = _recent_reason_for(rel, collections=collections)
            # An episode is offered once per conversation, by
            # `_recent_episodes`, never per file.
            if why and why != "episode":
                offers.append((mtime, rel, why))
        if after_burst is None and mtime > 0 and _counts_toward_burst(rel):
            if size and last - mtime <= HOT_PROFILE_BURST_GAP_NS:
                size += 1
            else:
                top, size = mtime, 1
            last = mtime
            if size >= HOT_PROFILE_BURST_PAGES:
                after_burst = top
    cutoff = after_burst or 0
    # A captured session is exempt: it is the record of what was spoken
    # about, not an edit a batch made, so a save written after it (three
    # pages a second apart is a burst) must not cut it from the block.
    return {rel: why for mtime, rel, why in offers if why == "captured" or mtime > cutoff}


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


def _recent_collection_dirs(paths: Iterable[str]) -> frozenset[str]:
    """Every directory that holds a `_collection.md`, from the paths in hand.

    Derived from the paths this request already holds — the freshness map and
    the index's anchor rows — rather than by asking the filesystem whether a
    sibling manifest exists: between them they name every page the block can
    offer, so this is string work on the request path. Both sources matter:
    without a watcher the map is empty and the rows are all there is.
    """
    marker = "/_collection.md"
    return frozenset(rel[: -len(marker)] for rel in paths if rel.endswith(marker))


_EPISODE_PREFIX = f"Sources/{source_taxonomy.EPISODE_PATH_LABEL}/"


def _recent_reason_for(rel: str, *, collections: frozenset[str] = frozenset()) -> str:
    """`edited`, `captured`, `episode`, or `""` for a page that is not working context.

    Raw material and operational state are excluded by the SAME path rules the
    content corpus already uses (`find_corpus.EXCLUDED_DIR_NAMES`,
    `NAVIGATION_BASENAMES`), so nothing here is a second, drifting opinion
    about what counts as a page. Two exclusions carry their own weight:
    `Sources/` is evidence ABOUT work rather than the work, except
    `Sources/Sessions/`, which IS the record of a conversation, and
    `Sources/Episodes/`, which is a conversation's recorded recap (a reserved
    folder, so this stays a string test); and the KB's
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
    if inner.startswith(_EPISODE_PREFIX):
        return "episode"
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
    from . import usage

    activations = _activation_snapshot()
    if not activations:
        return ()
    scored: list[tuple[float, str]] = []
    for rel in dict.fromkeys((*by_path, *mtimes)):
        # An episode is offered by `_recent_episodes`, newest revision only; a
        # read of a retired revision must not bring it back beside the newest.
        if _recent_reason_for(rel, collections=collections) in ("", "episode"):
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


def _recent_episodes(mtimes: Mapping[str, int], *, limit: int) -> tuple[str, ...]:
    """The newest revision of each of the most recent episodes, newest first.

    String work over the freshness map the block already copied: no read, no
    walk. Revisions of one episode share the filename's group token and are
    ordered by its recording-time token, never by mtime — retiring an older
    revision rewrites its frontmatter, so the retired file is often the
    fresher one. Grouping happens BEFORE egress, so a withheld newest revision
    makes its episode vanish rather than fall back to an older one. A file in
    the folder without the token is its own episode.
    """
    from . import episode_capture

    newest: dict[str, tuple[str, str]] = {}
    for rel in mtimes:
        if _recent_reason_for(rel) != "episode":
            continue
        parts = episode_capture.filename_parts(rel.rsplit("/", 1)[-1])
        group, order = (parts[0], parts[1]) if parts is not None else (rel, "")
        if group not in newest or (order, rel) > newest[group]:
            newest[group] = (order, rel)
    chosen = sorted((rel for _order, rel in newest.values()), key=lambda rel: (-mtimes[rel], rel))
    return tuple(chosen[:limit])


def _recent_episode_fields(vault_root: Path, rel: str) -> dict[str, str]:
    """An episode entry's authored `title`, `summary` statement and `episode` key.

    One cached page read, the one every other chosen entry gets for its
    statement. Anything absent or malformed is simply left out: the entry
    keeps its filename title and carries no statement.
    """
    from . import episode_capture, find_corpus

    try:
        page = find_corpus.CACHE.get(Path(vault_root) / rel, Path(vault_root))
    except Exception:  # noqa: BLE001 - an unreadable page costs its fields only
        log.debug("recent context: episode unreadable for %s", rel, exc_info=True)
        return {}
    frontmatter = getattr(page, "frontmatter", None)
    if not isinstance(frontmatter, dict):
        return {}
    fields: dict[str, str] = {}
    title = frontmatter.get("title")
    if isinstance(title, str) and title.strip():
        fields["title"] = " ".join(title.split())
    summary = frontmatter.get("summary")
    if isinstance(summary, str) and summary.strip():
        fields["statement"] = f"summary: {summary.strip()}"[: working_set_state.STATEMENT_MAX_CHARS]
    key = frontmatter.get("episode")
    if isinstance(key, str) and episode_capture.EPISODE_KEY_RE.fullmatch(key):
        fields["episode"] = key
    return fields


def _lifecycle_statuses() -> frozenset[str]:
    """Every page lifecycle status this tree defines: the per-type enums
    `note` validates against, plus the inactive ones `activation` retires."""
    from . import activation, note

    return frozenset(
        (*note.STATUS_BASIC, *note.STATUS_EXPERIMENT, *note.STATUS_PRODUCTION)
    ) | frozenset(activation._INACTIVE_STATUSES)


def _recent_frontmatter_statement(vault_root: Path, rel: str) -> str:
    """The page's authored `summary`, else a `status` that is not a
    lifecycle word, or `""`.

    Bounded to the entries the block already chose — at most
    `RECENT_CONTEXT_MAX_ENTRIES` pages, through the shared page cache, never a
    scan. Authored values only, rendered the way `working_set_state` renders
    them, so nothing here is a sentence the server wrote. A lifecycle status
    ("active", "draft", "concluded") says where the page is in its life, not
    what it says: rendered as the statement, "status: active" stood in for
    the summary the author wrote beside it.
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
    for name in ("summary", "status"):
        value = frontmatter.get(name)
        if not isinstance(value, (str, int, float)) or not str(value).strip():
            continue
        if name == "status" and working_set_index.normalize(str(value)) in _lifecycle_statuses():
            continue
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
