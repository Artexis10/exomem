"""The 33 pre-registered assertions, as deterministic functions over snapshots.

Every assertion takes an :class:`AssertionContext` — one snapshot, or a
snapshot pair for the transition invariants — and returns an
:class:`AssertionResult`. Nothing here reads a clock, a network, or a provider
internal, so a result is a pure function of the fixture that produced it.

Three rules hold across all thirty-three:

1. **Acceptance predicates, not implementations.** PREREGISTRATION §4 requires
   at least two structurally different representations to satisfy each
   invariant. The alternates are tried in order and the winning representation
   is named in the evidence string, so a pass says *how* the product satisfied
   the invariant rather than merely that it did.

2. **Absence is typed, never zero.** ``not_applicable`` comes only from a
   capability declaration of ``absent_by_design``; a projector that cannot
   observe the field at all yields ``unsupported``. This mirrors the three
   honesty tiers in :mod:`membench.scoring.health`, where an underivable
   measurement stays ``None`` rather than becoming a zero. A product whose own
   materials claim the property scores ``fail`` with the claim cited, never
   ``not_applicable``.

3. **One text-matching rule.** Where an assertion has to ask whether some text
   states a value, it calls :func:`membench.scoring.gates.states_value` — the
   harness's single fixed matching rule — and never bare substring containment.
   Structural comparisons (did this item's content change?) use a content hash,
   which is identity rather than text matching.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any, Literal, TypeVar

from membench.scoring.gates import states_value
from pydantic import Field

from .budgets import (
    CONTINUATION_PACKET_UNIT_BUDGET,
    ENTITY_EMERGENCE_SOURCE_BUDGET,
    RESTRUCTURE_QUIET_WINDOW_PASSES,
    STRUCTURAL_EMERGENCE_CLUSTER_BUDGET,
)
from .snapshot import CollectionProjection, EpistemicStateSnapshot, StateItem, StrictModel

#: The five-valued outcome vocabulary from the spec.
Outcome = Literal["pass", "fail", "not_applicable", "unsupported", "blocked"]

#: Item kinds that assert something and therefore owe an evidence path.
BELIEF_KINDS: frozenset[str] = frozenset(
    {"claim", "decision", "hypothesis", "derived_inference"}
)

#: Edges that count as an evidence hop, in PROV-O terms.
EVIDENCE_PREDICATES: frozenset[str] = frozenset({"cites", "derived_from", "evidenced_by"})

#: Review vocabularies. Providers name these differently, so the comparison is
#: over a normalized token rather than an exact string.
CLOSED_REVIEW_STATES: frozenset[str] = frozenset(
    {"closed", "dismissed", "resolved", "snoozed", "accepted", "done", "ignored"}
)
OPEN_REVIEW_STATES: frozenset[str] = frozenset(
    {"open", "reopened", "needs_review", "needs-review", "pending", "conflict", "flagged"}
)
#: Review states that mark a recorded conflict. A *closed vocabulary*, not a
#: text match: ``states_value("conflict", "no conflict")`` is true, so matching
#: the word inside a review-state field turned an explicit "no conflict" into
#: evidence of a visible contradiction.
CONFLICT_REVIEW_STATES: frozenset[str] = frozenset(
    {"conflict", "conflicted", "conflicting", "contradiction", "contradicted", "disputed"}
)
#: Review states that declare unresolved confidence.
UNCERTAIN_REVIEW_STATES: frozenset[str] = frozenset(
    {"uncertain", "unverified", "unconfirmed", "provisional"}
)

#: Collection/folder path segments that name a settled decision, and ones that
#: name something still open. Closed vocabularies, matched case-insensitively
#: against a single path segment — the same discipline as the review-state
#: vocabularies above and for the same reason: two items merely filed in
#: *different* folders say nothing about which one is decided, so accepting any
#: difference turned the documented-convention alternate into a diff detector.
DECISION_COLLECTION_TERMS: frozenset[str] = frozenset({"decision", "decisions"})
HYPOTHESIS_COLLECTION_TERMS: frozenset[str] = frozenset(
    {"hypothesis", "hypotheses", "proposal", "proposals"}
)

#: Vocabularies for the 2026-08 loop-closure families (f15-f19).
#:
#: Same discipline as the review-state vocabularies above, and for the same
#: reason: these are *closed* sets matched against a single field or attribute
#: value, never bare substring containment over prose. A projector that surfaces
#: a product's own wording maps it onto one of these tokens; anything else is
#: not observed rather than quietly accepted.

#: Attribute keys under which a projector records a prediction unit.
PREDICTION_RAW_KEYS: frozenset[str] = frozenset(
    {"prediction", "prediction_id", "predicts", "forecast"}
)
#: Attribute keys carrying the prediction's window or deadline.
DUE_RAW_KEYS: frozenset[str] = frozenset(
    {"due", "due_at", "deadline", "prediction_window", "resolve_by", "window_ends"}
)
#: Values that state the window has elapsed.
DUE_STATES: frozenset[str] = frozenset(
    {"due", "overdue", "past_due", "elapsed", "expired", "ready_for_review"}
)
#: Attribute keys under which a verdict is recorded.
VERDICT_RAW_KEYS: frozenset[str] = frozenset(
    {"verdict", "outcome", "resolution", "adjudication"}
)
#: Values that state a verdict was reached, either way.
VERDICT_STATES: frozenset[str] = frozenset(
    {"confirmed", "supported", "refuted", "falsified", "disconfirmed", "inconclusive"}
)
#: The subset of verdicts that are negative results. f18 is about these.
REFUTED_STATES: frozenset[str] = frozenset(
    {"refuted", "falsified", "disconfirmed", "disproven"}
)
#: Attribute keys naming a plan artifact, and values that mark a divergence.
PLAN_RAW_KEYS: frozenset[str] = frozenset({"plan", "plan_id", "intent"})
DIVERGENCE_STATES: frozenset[str] = frozenset(
    {"divergence", "diverged", "drift", "off_plan", "deviation", "mismatch"}
)
#: Attribute keys carrying a journey stage and the session it happened in.
STAGE_RAW_KEYS: frozenset[str] = frozenset({"stage", "journey_stage", "step"})
SESSION_RAW_KEYS: frozenset[str] = frozenset({"session", "session_id", "run"})
#: The loop stages f19 requires evidence of, in order.
JOURNEY_STAGES: tuple[str, ...] = (
    "goal",
    "hypothesis",
    "prediction",
    "intervention",
    "records",
    "review",
    "revision",
)

#: Vocabularies for the 2026-08 no-nudge families (f20-f26).
#:
#: Same closed-set discipline as everything above, and one addition of its own:
#: these families are *behaviour-not-vocabulary* by contract, so none of these
#: tokens may ever be matched against fixture prose. They are matched against a
#: single structured attribute value, and the f20 generator separately asserts
#: that no cluster-name token appears in any assertion parameter. A detector
#: that learned the fixture's words rather than its structure fails the family.

#: Attribute keys under which a projector records what class of signal an item
#: is. A "signal" here is any autonomously surfaced queue entry.
SIGNAL_CLASS_RAW_KEYS: frozenset[str] = frozenset(
    {"signal_class", "signal", "signal_kind", "candidate_kind"}
)
#: Signal classes that propose promoting or splitting accumulated structure.
#: f20's positive requires one of these; its twins forbid all of them.
PROMOTION_SIGNAL_CLASSES: frozenset[str] = frozenset(
    {"promotion", "promote", "structural", "structure", "split", "extract", "emergence"}
)
#: Signal classes that propose a recurring identity as an entity candidate.
ENTITY_SIGNAL_CLASSES: frozenset[str] = frozenset(
    {"entity_candidate", "entity", "identity_candidate", "identity"}
)
#: Signal classes that surface a contradiction or invalidation pair.
CONTRADICTION_SIGNAL_CLASSES: frozenset[str] = frozenset(
    {"contradiction", "conflict", "invalidation", "divergence_pair"}
)
#: Signal classes that propose folding items back together. f25 forbids these
#: against the children a just-applied restructure created.
MERGE_SIGNAL_CLASSES: frozenset[str] = frozenset(
    {"merge", "consolidation", "consolidate", "combine", "fold"}
)
#: Every class that counts as promotion-class for the false-positive ceiling.
#: The ceiling is about *any* unsolicited promotion of a twin, so entity and
#: structural proposals both count; a merge proposal does not, because f25
#: measures that separately and against different subjects.
UNSOLICITED_PROPOSAL_CLASSES: frozenset[str] = PROMOTION_SIGNAL_CLASSES | ENTITY_SIGNAL_CLASSES

#: Every signal class this amendment knows about. A quiet assertion that proves
#: absence over *this* is proving absence of nagging as such, rather than of one
#: dialect of it.
ALL_SIGNAL_CLASSES: frozenset[str] = (
    PROMOTION_SIGNAL_CLASSES
    | ENTITY_SIGNAL_CLASSES
    | CONTRADICTION_SIGNAL_CLASSES
    | MERGE_SIGNAL_CLASSES
)

#: ``assertion name -> the signal classes it proves absence over``. Populated by
#: :func:`claims_absence` where each predicate is defined, so the vocabulary a
#: quiet assertion is answerable for is stated next to its implementation rather
#: than inferred by whoever reads it later.
ABSENCE_CLAIM_CLASSES: dict[str, frozenset[str]] = {}

#: ``family -> extra signal classes its quiet assertion must also prove absent``.
#:
#: f22 is why this exists. Its twin's quiet assertion is the meta-predicate
#: itself, so there is no family-specific callable to carry the declaration, and
#: with only the promotion-class vocabulary a product that surfaced *every*
#: similar pair as a contradiction produced false positives no assertion in the
#: family could see. The mapping is code, not fixture data: a scenario cannot
#: reach it, and it is unioned in, so a family can only ever be held to *more*
#: than the default — never less.
FAMILY_ABSENCE_CLASSES: Mapping[str, frozenset[str]] = MappingProxyType(
    {"f22": CONTRADICTION_SIGNAL_CLASSES}
)

_AssertionT = TypeVar("_AssertionT", bound=Callable[..., "AssertionResult"])


def claims_absence(*vocabularies: frozenset[str]) -> Callable[[_AssertionT], _AssertionT]:
    """Mark a predicate as an absence claim and declare its signal vocabulary.

    The marker is what ``COMPOSES_ABSENCE_META`` is checked against, so a future
    quiet assertion cannot be added to the registry while quietly staying out of
    the set that forces it to compose the meta-predicate. The declared classes
    are always unioned with :data:`UNSOLICITED_PROPOSAL_CLASSES`: a predicate may
    widen what it proves silence about, never narrow it.
    """

    def decorate(fn: _AssertionT) -> _AssertionT:
        fn.absence_claim = True  # type: ignore[attr-defined]
        ABSENCE_CLAIM_CLASSES[fn.__name__] = UNSOLICITED_PROPOSAL_CLASSES.union(*vocabularies)
        return fn

    return decorate


#: Attribute keys naming which surface an item was observed on.
SURFACE_RAW_KEYS: frozenset[str] = frozenset({"surface", "queue", "view"})
#: Attribute keys naming what an item is about.
TARGET_RAW_KEYS: frozenset[str] = frozenset({"targets", "target", "about", "subject_id"})
#: Attribute keys carrying a surface's projection completeness.
PROJECTION_RAW_KEYS: frozenset[str] = frozenset({"projection", "projected", "surface_state"})
#: The only projection value that permits a quiet assertion to conclude silence.
#: Anything else — including an explicitly empty projection — is an error.
PROJECTION_COMPLETE: str = "complete"

#: The surfaces on which absence must be proven before any quiet assertion may
#: pass. The counters block is in this list deliberately: without it, a product
#: that stops emitting a queue item but keeps naming the twin in its due-state
#: counters would pass a quiet assertion while still nagging the user.
DECLARED_ABSENCE_SURFACES: tuple[str, ...] = (
    "audit_findings",
    "review_queue",
    "proposal_queue",
    "due_state_counters",
)

#: Attribute keys carrying how many structurally distinct clusters have
#: accumulated on a note, and how many distinct sources an identity recurs in.
CLUSTER_COUNT_RAW_KEYS: frozenset[str] = frozenset({"cluster_count", "clusters"})
SOURCE_COUNT_RAW_KEYS: frozenset[str] = frozenset({"source_count", "sources", "distinct_sources"})
#: Attribute keys carrying a durable triage fingerprint and its decision.
FINGERPRINT_RAW_KEYS: frozenset[str] = frozenset(
    {"fingerprint", "signal_fingerprint", "dismissal_fingerprint"}
)
#: Attribute keys carrying how many maintenance passes an item has survived,
#: and how many writes a counters block was emitted for.
PASS_COUNT_RAW_KEYS: frozenset[str] = frozenset({"passes", "pass_count", "maintenance_passes"})
EMISSION_COUNT_RAW_KEYS: frozenset[str] = frozenset({"emissions", "emission_count"})
WRITE_COUNT_RAW_KEYS: frozenset[str] = frozenset({"writes", "write_count", "batch_size"})
#: How big the LAST DELIVERED block was. Informational only: it persists across
#: batches, so it cannot say whether the batch under test delivered anything —
#: that is the emission delta's job (see
#: `counter_emission_not_repeated_per_write`).
DUE_TOTAL_RAW_KEYS: frozenset[str] = frozenset({"due_total", "outstanding_total"})
#: Attribute keys marking an item as the continuation packet, and the response
#: detail level a carrier journey ran at.
PACKET_RAW_KEYS: frozenset[str] = frozenset({"packet", "continuation_packet", "packet_id"})
RESPONSE_DETAIL_RAW_KEYS: frozenset[str] = frozenset({"response_detail", "detail"})
#: Attribute key marking an item as a foreign-project decoy the packet must
#: exclude entirely, and one marking a restructure's newly created child.
DECOY_RAW_KEYS: frozenset[str] = frozenset({"decoy", "foreign_project"})
RESTRUCTURE_CHILD_RAW_KEYS: frozenset[str] = frozenset({"restructure_child", "child_of"})

#: Cap on how many offenders one evidence string lists (as the gates do).
_MAX_LISTED = 8


class AssertionResult(StrictModel):
    """One deterministic verdict. Final by contract: no judge may overturn it."""

    name: str = Field(min_length=1)
    outcome: Outcome
    evidence: str = Field(min_length=1)
    subject: str | None = None


@dataclass(frozen=True)
class AssertionContext:
    """Everything an assertion may read.

    ``snapshot`` is the snapshot under test. ``prior`` is the earlier snapshot
    for transition invariants (and the *live* snapshot when the assertion
    compares an export against it). Every parameter is caller-supplied so a
    result never depends on ambient state.
    """

    snapshot: EpistemicStateSnapshot
    prior: EpistemicStateSnapshot | None = None
    subject: str | None = None
    counterpart: str | None = None
    #: Ids a retrieval surface actually served, when the scenario captured them.
    served_items: tuple[str, ...] | None = None
    #: Foreign-case canary hits from the isolation probe; ``None`` = not run.
    foreign_case_hits: tuple[str, ...] | None = None
    #: Declared freshness bound for external-edit adoption, in seconds.
    freshness_bound_s: float | None = None
    #: When the out-of-band edit happened, RFC3339, caller-supplied.
    external_edit_at: str | None = None
    #: Allowed reconstruction loss for the export comparison, 0.0 - 1.0.
    tolerance: float = 0.0
    #: The scenario's family, supplied by the runner. Read only to widen a quiet
    #: assertion's signal vocabulary (see :data:`FAMILY_ABSENCE_CLASSES`); it can
    #: never narrow one, so it cannot be used to make an assertion easier.
    family: str | None = None

    @property
    def absence_surfaces(self) -> tuple[str, ...]:
        """The surfaces this context must prove absence on.

        Fixed, not negotiable: the canonical four in
        :data:`DECLARED_ABSENCE_SURFACES`. An earlier version let a caller widen
        this list, but nothing ever did, and a widening path no scenario reaches
        is a claim about rigour rather than rigour — so the list is simply the
        registered one and a scenario has no say in it.
        """

        return DECLARED_ABSENCE_SURFACES

    def replace(self, **changes: object) -> AssertionContext:
        return replace(self, **changes)  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# Result helpers
# --------------------------------------------------------------------------


def _result(name: str, outcome: Outcome, evidence: str, subject: str | None) -> AssertionResult:
    return AssertionResult(name=name, outcome=outcome, evidence=evidence, subject=subject)


def _listed(values: Iterable[str]) -> str:
    ordered = sorted(values)
    head = ordered[:_MAX_LISTED]
    suffix = f" (+{len(ordered) - _MAX_LISTED} more)" if len(ordered) > _MAX_LISTED else ""
    return ", ".join(head) + suffix


def _content_hash(item: StateItem) -> str:
    """Identity of an item's material content; not a text-matching rule."""

    payload = f"{item.title}\x00{item.text}".encode()
    return hashlib.sha256(payload).hexdigest()


def _normalize_state(state: str | None) -> str:
    return (state or "").strip().casefold()


def _is_closed(state: str | None) -> bool:
    return _normalize_state(state) in CLOSED_REVIEW_STATES


def _is_open(state: str | None) -> bool:
    return _normalize_state(state) in OPEN_REVIEW_STATES


def _is_uncertain(state: str | None) -> bool:
    return _normalize_state(state) in UNCERTAIN_REVIEW_STATES


def _conflict_marker(item: StateItem) -> bool:
    """A conflict marker on the item, read from the closed review vocabulary."""

    return _normalize_state(item.review_state) in CONFLICT_REVIEW_STATES


def _folder(locator: str | None) -> str:
    if not locator or "/" not in locator:
        return ""
    return locator.rsplit("/", 1)[0]


def _collection_terms(locator: str | None) -> tuple[bool, bool]:
    """``(names a decision collection, names a hypothesis/proposal collection)``.

    Reads the *segments* of the containing folder against the closed
    vocabularies, case-insensitively. Segment-level matching is deliberate: it
    keeps ``Notes/Decisions`` a hit while leaving ``Notes/Decision-Log-Archive``
    a miss rather than guessing at substrings.
    """

    segments = {
        segment.strip().casefold()
        for segment in _folder(locator).split("/")
        if segment.strip()
    }
    return (
        bool(segments & DECISION_COLLECTION_TERMS),
        bool(segments & HYPOTHESIS_COLLECTION_TERMS),
    )


def _elapsed_seconds(start: str, end: str) -> float | None:
    try:
        first = datetime.fromisoformat(start)
        second = datetime.fromisoformat(end)
    except ValueError:
        return None
    if (first.tzinfo is None) != (second.tzinfo is None):
        return None
    return (second - first).total_seconds()


# --------------------------------------------------------------------------
# Capability gate: three honesty tiers before any structural evaluation
# --------------------------------------------------------------------------


def _absence_result(name: str, declaration, subject: str | None) -> AssertionResult:
    """``not_applicable`` for a designed absence — or ``fail`` if it is claimed."""

    if declaration.marketing_claim:
        return _result(
            name,
            "fail",
            f"'{declaration.field}' is declared absent_by_design, but the product's own "
            f"materials claim it: {declaration.marketing_claim} "
            f"(declaration evidence: {declaration.evidence})",
            subject,
        )
    return _result(
        name,
        "not_applicable",
        f"'{declaration.field}' declared absent_by_design ({declaration.evidence})",
        subject,
    )


def _gate(
    ctx: AssertionContext,
    name: str,
    primary: str,
    *siblings: str,
) -> AssertionResult | None:
    """``None`` when the invariant is observable; otherwise the honest non-pass.

    Every assertion names one **primary** field — the capability the invariant
    is actually about — plus any number of siblings that can widen how it is
    observed. The asymmetry is deliberate and was a real bug before it existed:
    with a flat OR over fields, a product that declared ``external_edit``
    ``absent_by_design`` still got evaluated (and could take a *catastrophic
    failure*) purely because the unrelated ``locator`` field happened to be
    declared. A designed absence of the primary field now short-circuits, and
    no sibling can override it.

    Siblings only ever act in the observable direction: they can make an
    otherwise unobservable invariant evaluable, never the reverse.

    Otherwise this mirrors :mod:`membench.scoring.health` — declared capability
    is evaluated, designed absence is ``not_applicable``, anything the projector
    cannot see is ``unsupported``, and nothing silently becomes a zero.
    """

    fields = (primary, *siblings)
    primary_declaration = ctx.snapshot.declaration(primary)

    # A designed absence of the primary capability decides on its own.
    if primary_declaration is not None and primary_declaration.status == "absent_by_design":
        return _absence_result(name, primary_declaration, ctx.subject)

    declarations = [
        declaration
        for declaration in (ctx.snapshot.declaration(field) for field in fields)
        if declaration is not None
    ]
    if any(declaration.observable for declaration in declarations):
        return None

    by_design = [d for d in declarations if d.status == "absent_by_design"]
    claimed = [d for d in by_design if d.marketing_claim]
    if claimed:
        return _absence_result(name, claimed[0], ctx.subject)
    if by_design:
        return _absence_result(name, by_design[0], ctx.subject)
    if declarations:
        declaration = declarations[0]
        return _result(
            name,
            "unsupported",
            f"'{declaration.field}' declared unavailable to the projector "
            f"({declaration.evidence})",
            ctx.subject,
        )
    return _result(
        name,
        "unsupported",
        f"no capability declaration for {', '.join(fields)}; "
        "an undeclared field is unsupported, never a zero",
        ctx.subject,
    )


# --------------------------------------------------------------------------
# Lineage helpers
# --------------------------------------------------------------------------


def _superseded_ids(snapshot: EpistemicStateSnapshot) -> set[str]:
    superseded = {item.revision_of for item in snapshot.items if item.revision_of}
    superseded |= {
        relation.object for relation in snapshot.relations if relation.predicate == "supersedes"
    }
    return {value for value in superseded if value}


def _union_roots(snapshot: EpistemicStateSnapshot) -> dict[str, str]:
    """item id -> lineage root, unioned over ``revision_of`` and supersedes edges."""

    parent: dict[str, str] = {item.id: item.id for item in snapshot.items}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    known = set(parent)
    for item in snapshot.items:
        if item.revision_of and item.revision_of in known:
            union(item.id, item.revision_of)
    for relation in snapshot.relations:
        if (
            relation.predicate == "supersedes"
            and relation.subject in known
            and relation.object in known
        ):
            union(relation.subject, relation.object)
    return {item.id: find(item.id) for item in snapshot.items}


def _revision_groups(snapshot: EpistemicStateSnapshot) -> dict[str, tuple[StateItem, ...]]:
    """Group items into revision chains, keyed on the lineage root.

    Keyed on the *root*, never on the declared ``revision_chain_id``: two
    genuinely independent chains that both label themselves with the same chain
    id used to collide in this dict, and the later one silently overwrote the
    earlier — which meant a chain with two current revisions could vanish from
    the evaluation entirely. The declared chain id is still honoured for
    *subject matching* in :func:`exactly_one_current_revision`, where it is a
    label the scenario can name, not an identity the engine trusts.
    """

    roots = _union_roots(snapshot)
    by_root: dict[str, list[StateItem]] = {}
    for item in snapshot.items:
        by_root.setdefault(roots[item.id], []).append(item)

    groups: dict[str, tuple[StateItem, ...]] = {}
    for root, members in by_root.items():
        is_chain = len(members) > 1 or any(
            m.revision_of or m.retired_reason or m.revision_chain_id for m in members
        )
        if not is_chain:
            continue
        groups[root] = tuple(sorted(members, key=lambda m: m.id))
    return groups


def _lineage_members(
    snapshot: EpistemicStateSnapshot, item_id: str | None
) -> tuple[StateItem, ...]:
    """Items sharing ``item_id``'s lineage (itself included); empty if unknown."""

    if not item_id:
        return ()
    roots = _union_roots(snapshot)
    root = roots.get(item_id)
    if root is None:
        return ()
    return tuple(item for item in snapshot.items if roots[item.id] == root)


def _evidence_targets(snapshot: EpistemicStateSnapshot, item: StateItem) -> tuple[str, ...]:
    targets = list(item.cites)
    for relation in snapshot.relations:
        if relation.subject == item.id and relation.predicate in EVIDENCE_PREDICATES:
            targets.append(relation.object)
    seen: list[str] = []
    for target in targets:
        if target not in seen:
            seen.append(target)
    return tuple(seen)


def _raw_value(item: StateItem, keys: frozenset[str]) -> tuple[str, str] | None:
    """The first ``(key, value)`` whose key is in ``keys``; ids stay sorted-stable."""

    for key, value in sorted(item.raw.items()):
        if key.strip().lower() in keys:
            return key, value
    return None


def _raw_states(item: StateItem, keys: frozenset[str], vocabulary: frozenset[str]) -> str | None:
    """The vocabulary token a documented attribute states, or ``None``.

    Two closed sets, never a free-text scan: the attribute must be one the
    projector documented, and its value must state one of the tokens under the
    harness's single fixed matching rule.
    """

    found = _raw_value(item, keys)
    if found is None:
        return None
    _key, value = found
    for token in sorted(vocabulary):
        if states_value(token, value) or states_value(token.replace("_", "-"), value):
            return token
    return None


def _prediction_items(snapshot: EpistemicStateSnapshot) -> tuple[StateItem, ...]:
    """Items a projector recorded as prediction units."""

    return tuple(
        item
        for item in snapshot.items
        if _raw_value(item, PREDICTION_RAW_KEYS) is not None
        or _raw_value(item, DUE_RAW_KEYS) is not None
    )


def _hypothesis_items(snapshot: EpistemicStateSnapshot) -> tuple[StateItem, ...]:
    """Hypotheses and prediction units — whatever a verdict may adjudicate."""

    predictions = {item.id for item in _prediction_items(snapshot)}
    return tuple(
        item
        for item in snapshot.items
        if item.kind == "hypothesis" or item.id in predictions
    )


def _verdict_of(snapshot: EpistemicStateSnapshot, item: StateItem) -> tuple[str, str] | None:
    """``(verdict, how)`` for ``item``: on the item, or on a linked outcome.

    The two alternates PREREGISTRATION §4 requires. The linked form must
    dereference — an outcome artifact that names no adjudicated subject, or
    names one that is not in the snapshot, is not a retrievable verdict.
    """

    direct = _raw_states(item, VERDICT_RAW_KEYS, VERDICT_STATES)
    if direct is not None:
        return direct, f"verdict attribute on {item.id}"
    for candidate in snapshot.items:
        if candidate.id == item.id:
            continue
        linked = _raw_states(candidate, VERDICT_RAW_KEYS, VERDICT_STATES)
        if linked is None:
            continue
        resolves = item.id in _evidence_targets(snapshot, candidate) or any(
            relation.subject == candidate.id
            and relation.object == item.id
            and relation.predicate in {"answers", "supports", "contradicts", "relates_to"}
            for relation in snapshot.relations
        )
        if resolves:
            return linked, f"linked outcome {candidate.id} adjudicating {item.id}"
    return None


def _support_roots(snapshot: EpistemicStateSnapshot, item: StateItem) -> frozenset[str]:
    """Raw-source ids reachable from ``item`` by evidence hops, bounded.

    Bounded by the snapshot's own item count so a cyclic provenance graph is a
    terminating traversal rather than a hang.
    """

    seen: set[str] = set()
    roots: set[str] = set()
    frontier = [item.id]
    limit = len(snapshot.items) + 1
    while frontier and len(seen) <= limit:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        node = snapshot.item(current)
        if node is None:
            continue
        targets = _evidence_targets(snapshot, node)
        if node.kind == "raw_source" and node.id != item.id:
            roots.add(node.id)
            continue
        frontier.extend(targets)
    return frozenset(roots)


def _outgoing_edges(
    snapshot: EpistemicStateSnapshot, item_id: str
) -> frozenset[tuple[str, str]]:
    """``(predicate, object)`` for every edge leaving ``item_id``."""

    return frozenset(
        (relation.predicate, relation.object)
        for relation in snapshot.relations
        if relation.subject == item_id
    )


def _belief_items(ctx: AssertionContext) -> tuple[StateItem, ...]:
    items = tuple(
        item
        for item in ctx.snapshot.items
        if item.kind in BELIEF_KINDS and item.current != "no"
    )
    if ctx.subject:
        items = tuple(item for item in items if item.id == ctx.subject)
    return items


def _pair(ctx: AssertionContext) -> tuple[StateItem | None, StateItem | None]:
    left = ctx.snapshot.item(ctx.subject) if ctx.subject else None
    right = ctx.snapshot.item(ctx.counterpart) if ctx.counterpart else None
    return left, right


def _declared_but_unobservable(
    ctx: AssertionContext, name: str, *, check_counterpart: bool = False
) -> AssertionResult | None:
    """``unsupported`` when a declared subject/counterpart is not in the snapshot.

    "Nothing was declared" and "what was declared is not there" are different
    facts, and every assertion that collapsed them leaked the same way: the
    unresolvable name fell through to a snapshot-wide reading, or to some
    substituted item, and answered a question about state nobody could observe.
    Once with a catastrophic assertion (see R-B1b) and twice more besides.

    So a declared name that does not resolve is reported as exactly that. This
    is also the only honest answer: an id-shape mismatch between a projector
    (vault-relative paths) and a scenario fixture (logical ids) is a harness
    problem, and scoring it as either a pass or a fail would attribute a
    harness problem to the provider.

    ``check_counterpart`` is opt-in because the counterpart does not mean the
    same thing everywhere. Where it names a co-equal item under comparison, an
    absent one is unobservable. Where it names an item whose *absence is the
    finding* — ``prior_revision_retained``'s declared predecessor — this guard
    must stay off, or it would convert destroyed history into ``unsupported``.
    """

    candidates = [("subject", ctx.subject)]
    if check_counterpart:
        candidates.append(("counterpart", ctx.counterpart))
    for label, declared in candidates:
        if declared and ctx.snapshot.item(declared) is None:
            return _result(
                name,
                "unsupported",
                f"declared {label} {declared!r} is not observable in this snapshot",
                ctx.subject,
            )
    return None


# --------------------------------------------------------------------------
# 1-2: currency
# --------------------------------------------------------------------------


def exactly_one_current_revision(ctx: AssertionContext) -> AssertionResult:
    """Each revision chain exposes exactly one current revision."""

    name = "exactly_one_current_revision"
    gated = _gate(ctx, name, "current", "revision_of")
    if gated is not None:
        return gated

    groups = _revision_groups(ctx.snapshot)
    if ctx.subject:
        # The subject may name a lineage root, a member item, or the chain id
        # the provider declared. All three are legitimate scenario handles.
        groups = {
            key: members
            for key, members in groups.items()
            if key == ctx.subject
            or any(
                member.id == ctx.subject or member.revision_chain_id == ctx.subject
                for member in members
            )
        }
    if not groups:
        return _result(
            name,
            "unsupported",
            "no revision lineage observed; currency of a chain cannot be evaluated",
            ctx.subject,
        )

    offenders = []
    for key in sorted(groups):
        current = [member.id for member in groups[key] if member.current == "yes"]
        if len(current) != 1:
            offenders.append(f"{key}: {len(current)} current [{_listed(current) or 'none'}]")
    if offenders:
        return _result(name, "fail", f"chains without a single current revision: {'; '.join(offenders)}", ctx.subject)
    return _result(
        name,
        "pass",
        f"{len(groups)} revision chain(s) each expose exactly one current revision: {_listed(groups)}",
        ctx.subject,
    )


def no_retired_state_served_as_current(ctx: AssertionContext) -> AssertionResult:
    """Retired state is never served as, or marked, current. CATASTROPHIC.

    **Scope extension, amendment sequence 2 (2026-08-16).** A continuation
    packet is a serving surface: handing a fresh session a retired decision by
    reference is the same harm as returning it from a query, and f24 would
    otherwise measure packet *completeness* while saying nothing about packet
    *currency*. So the packet's referenced units are treated as served items.
    This is an extension of an existing catastrophic assertion's scope, stated
    explicitly in §7 rather than smuggled in, and it adds no new catastrophic
    assertion — the §3 set is unchanged.
    """

    name = "no_retired_state_served_as_current"
    gated = _gate(ctx, name, "current")
    if gated is not None:
        return gated

    snapshot = ctx.snapshot
    by_id = snapshot.items_by_id()
    superseded = _superseded_ids(snapshot)

    def retired(item: StateItem) -> bool:
        return item.current == "no" or bool(item.retired_reason) or item.id in superseded

    packet = _packet_item(snapshot)
    if packet is not None:
        packet_served = sorted(_packet_referenced_ids(snapshot, packet))
        stale = [
            f"{item_id} (current={item.current}, retired_reason={item.retired_reason!r})"
            for item_id in packet_served
            if (item := by_id.get(item_id)) is not None and retired(item)
        ]
        if stale:
            return _result(
                name,
                "fail",
                f"continuation packet {packet.id} serves retired state as current: "
                f"{_listed(stale)}",
                ctx.subject,
            )

    if ctx.served_items is not None:
        offenders: list[str] = []
        unresolved: list[str] = []
        for served in ctx.served_items:
            item = by_id.get(served)
            if item is None:
                unresolved.append(served)
                continue
            if retired(item):
                offenders.append(
                    f"{served} (current={item.current}, retired_reason={item.retired_reason!r})"
                )
        if offenders:
            return _result(
                name,
                "fail",
                f"retrieval served retired state: {_listed(offenders)}",
                ctx.subject,
            )
        note = f"; {len(unresolved)} served id(s) not present in the snapshot" if unresolved else ""
        return _result(
            name,
            "pass",
            f"{len(ctx.served_items)} served item(s) are all current{note}",
            ctx.subject,
        )

    if not snapshot.items:
        return _result(name, "unsupported", "empty snapshot; nothing observable to serve", ctx.subject)
    # ``undeclared`` counts alongside ``yes``: a retired item whose currency the
    # product never states is still offered as though it were live, and letting
    # silence exempt it would reward not answering the question.
    offenders = [
        f"{item.id} (current={item.current}, retired_reason={item.retired_reason!r})"
        for item in snapshot.items
        if item.current in {"yes", "undeclared"}
        and (item.retired_reason or item.id in superseded)
    ]
    if offenders:
        return _result(
            name,
            "fail",
            f"retired state is not marked retired: {_listed(offenders)}",
            ctx.subject,
        )
    return _result(
        name,
        "pass",
        "no served-item list captured; every retired or superseded item is marked "
        "non-current",
        ctx.subject,
    )


# --------------------------------------------------------------------------
# 3-4: lineage
# --------------------------------------------------------------------------


def prior_revision_retained(ctx: AssertionContext) -> AssertionResult:
    """History survives a correction. CATASTROPHIC.

    Accepts a resolving revision chain, a retained superseded artifact *in the
    subject's own lineage*, or a documented ``available_via:<mechanism>``
    history surface.

    The lineage scoping is the whole point. An earlier build accepted any
    retired item anywhere in the snapshot as proof that history was retained,
    which meant a product that destroyed the predecessor of the corrected
    conclusion still passed a catastrophic invariant because some unrelated
    note happened to be archived. Retention is a claim about *this* subject's
    predecessor, so the retained artifact must be reachable from the subject:
    same lineage (``revision_of`` / supersedes edge / shared chain id), or the
    predecessor the scenario itself declared via ``counterpart``.

    A ``revision_of`` that no longer dereferences is a **failure**, not a
    fall-through to the weaker alternates: the product recorded that it had a
    predecessor and then lost it, which is exactly the harm being measured.
    """

    name = "prior_revision_retained"
    gated = _gate(ctx, name, "prior_revision", "revision_of", "current")
    if gated is not None:
        return gated

    snapshot = ctx.snapshot
    by_id = snapshot.items_by_id()
    subject = snapshot.item(ctx.subject) if ctx.subject else None

    # An unresolvable subject must not fall through to the snapshot-wide
    # widening below — that was R-B1b, and it handed a catastrophic pass off any
    # retired item anywhere. The counterpart is deliberately NOT checked here:
    # for this assertion it names the declared predecessor, whose absence is the
    # finding rather than a reason to stop scoring.
    unobservable = _declared_but_unobservable(ctx, name)
    if unobservable is not None:
        return unobservable

    successors = (
        (subject,)
        if subject is not None
        else tuple(item for item in snapshot.items if item.revision_of)
    )
    for item in successors:
        if item is None or not item.revision_of:
            continue
        if item.revision_of in by_id:
            return _result(
                name,
                "pass",
                f"revision chain: {item.id} links to retained predecessor {item.revision_of}",
                ctx.subject,
            )
        return _result(
            name,
            "fail",
            f"history destroyed: {item.id} declares revision_of "
            f"{item.revision_of!r}, which no longer resolves to any item",
            ctx.subject,
        )

    # Representation #2, scoped: a retained superseded artifact that the subject
    # can actually be connected to.
    lineage = {member.id for member in _lineage_members(snapshot, ctx.subject)}
    if not ctx.subject:
        # Only an *undeclared* subject widens to every revision group. Past this
        # point an unresolvable subject has already returned unsupported.
        lineage = {
            member.id
            for members in _revision_groups(snapshot).values()
            for member in members
        }
    if ctx.counterpart:
        lineage.add(ctx.counterpart)
    lineage.discard(ctx.subject or "")
    retained = [
        item.id
        for item in snapshot.items
        if item.id in lineage and (item.current == "no" or item.retired_reason)
    ]
    if retained:
        scope = "the subject's lineage" if ctx.subject else "an observed revision lineage"
        return _result(
            name,
            "pass",
            f"retained superseded artifact(s) in {scope}: {_listed(retained)}",
            ctx.subject,
        )

    for field in ("prior_revision", "revision_of"):
        declaration = snapshot.declaration(field)
        if declaration is not None and declaration.mechanism:
            return _result(
                name,
                "pass",
                f"documented history mechanism available_via:{declaration.mechanism} "
                f"for '{field}' ({declaration.evidence})",
                ctx.subject,
            )

    return _result(
        name,
        "fail",
        "no prior revision retained for this lineage: no resolving revision chain, no "
        "retained superseded artifact reachable from the subject, and no declared "
        "history mechanism",
        ctx.subject,
    )


def revision_links_to_predecessor(ctx: AssertionContext) -> AssertionResult:
    """The current revision names what it replaced.

    Accepts an explicit supersession edge, an in-content reference naming the
    replaced item, or a version chain (chain id plus index).
    """

    name = "revision_links_to_predecessor"
    gated = _gate(ctx, name, "revision_of", "prior_revision")
    if gated is not None:
        return gated

    # A declared subject that does not resolve used to be silently replaced by
    # "the first item with a revision_of, else the first current item" — so the
    # verdict was about an item the scenario never named, and could be a pass.
    unobservable = _declared_but_unobservable(ctx, name)
    if unobservable is not None:
        return unobservable

    snapshot = ctx.snapshot
    subject = snapshot.item(ctx.subject) if ctx.subject else None
    if subject is None:
        # Nothing declared: picking the observed successor is the intended wide
        # reading, not a substitution.
        successors = [item for item in snapshot.items if item.revision_of] or [
            item for item in snapshot.items if item.current == "yes"
        ]
        subject = successors[0] if successors else None
    if subject is None:
        return _result(name, "unsupported", "no current revision observed", ctx.subject)

    if subject.revision_of:
        return _result(
            name,
            "pass",
            f"explicit supersession edge: {subject.id} revision_of {subject.revision_of}",
            subject.id,
        )
    for relation in snapshot.relations:
        if relation.predicate == "supersedes" and relation.subject == subject.id:
            return _result(
                name,
                "pass",
                f"typed supersedes relation: {subject.id} -> {relation.object}",
                subject.id,
            )

    # The in-content alternate only counts when the named item could actually be
    # this subject's predecessor. Scanning every retired item in the snapshot
    # let a body that happened to mention an unrelated archived note pass as a
    # supersession reference.
    predecessors: list[StateItem] = []
    if ctx.counterpart:
        candidate = snapshot.item(ctx.counterpart)
        if candidate is not None:
            predecessors.append(candidate)
    else:
        predecessors.extend(
            member
            for member in _lineage_members(snapshot, subject.id)
            if member.id != subject.id and (member.current == "no" or member.retired_reason)
        )
    for predecessor in predecessors:
        for token in (predecessor.title, predecessor.id):
            if token and states_value(token, subject.text):
                return _result(
                    name,
                    "pass",
                    f"in-content reference: {subject.id} names replaced item {token!r}",
                    subject.id,
                )

    if subject.revision_chain_id and subject.revision_index is not None:
        siblings = [
            item.id
            for item in snapshot.items
            if item.id != subject.id and item.revision_chain_id == subject.revision_chain_id
        ]
        if siblings:
            return _result(
                name,
                "pass",
                f"version chain {subject.revision_chain_id} index {subject.revision_index} "
                f"with sibling revision(s) {_listed(siblings)}",
                subject.id,
            )

    return _result(
        name,
        "fail",
        f"{subject.id} names no predecessor: no supersession edge, no in-content "
        "reference, and no version chain",
        subject.id,
    )


# --------------------------------------------------------------------------
# 5-6: evidence paths
# --------------------------------------------------------------------------


def evidence_path_exists(ctx: AssertionContext) -> AssertionResult:
    """Every promoted conclusion has at least one evidence hop."""

    name = "evidence_path_exists"
    gated = _gate(ctx, name, "cites")
    if gated is not None:
        return gated

    beliefs = _belief_items(ctx)
    if not beliefs:
        return _result(name, "unsupported", "no belief-bearing items observed", ctx.subject)
    missing = [item.id for item in beliefs if not _evidence_targets(ctx.snapshot, item)]
    if missing:
        return _result(
            name, "fail", f"conclusions with no evidence path: {_listed(missing)}", ctx.subject
        )
    return _result(
        name,
        "pass",
        f"{len(beliefs)} conclusion(s) each carry an evidence path",
        ctx.subject,
    )


def evidence_path_resolves(ctx: AssertionContext) -> AssertionResult:
    """Every hop dereferences to a live artifact. CATASTROPHIC.

    Vacuity is a failure here on purpose: erasing the evidence edges must not
    be a cheaper way to satisfy a catastrophic invariant than keeping them.
    """

    name = "evidence_path_resolves"
    gated = _gate(ctx, name, "cites")
    if gated is not None:
        return gated

    beliefs = _belief_items(ctx)
    if not beliefs:
        return _result(name, "unsupported", "no belief-bearing items observed", ctx.subject)

    snapshot = ctx.snapshot
    by_id = snapshot.items_by_id()
    dangling: list[str] = []
    empty: list[str] = []
    for belief in beliefs:
        seen: set[str] = {belief.id}
        frontier = list(_evidence_targets(snapshot, belief))
        if not frontier:
            empty.append(belief.id)
            continue
        while frontier:
            target = frontier.pop()
            if target in seen:
                continue
            seen.add(target)
            item = by_id.get(target)
            if item is None:
                dangling.append(f"{belief.id} -> {target}")
                continue
            frontier.extend(_evidence_targets(snapshot, item))
    if dangling or empty:
        parts = []
        if dangling:
            parts.append(f"unresolvable hops: {_listed(dangling)}")
        if empty:
            parts.append(f"conclusions with no path to dereference: {_listed(empty)}")
        return _result(name, "fail", "; ".join(parts), ctx.subject)
    return _result(
        name,
        "pass",
        f"every evidence hop from {len(beliefs)} conclusion(s) dereferences to a live item",
        ctx.subject,
    )


# --------------------------------------------------------------------------
# 7-8: contradiction
# --------------------------------------------------------------------------


def contradiction_visible(ctx: AssertionContext) -> AssertionResult:
    """A conflict is observable.

    Accepts a typed contradiction relation, both sides current with a conflict
    marker, or a review-queue entry citing both sides.
    """

    name = "contradiction_visible"
    gated = _gate(ctx, name, "contradicts")
    if gated is not None:
        return gated

    snapshot = ctx.snapshot
    left, right = _pair(ctx)
    if ctx.subject and ctx.counterpart:
        pair_ids = {ctx.subject, ctx.counterpart}
        if left is not None and ctx.counterpart in left.contradicts:
            return _result(name, "pass", f"typed contradiction edge {left.id} -> {ctx.counterpart}", ctx.subject)
        if right is not None and ctx.subject in right.contradicts:
            return _result(name, "pass", f"typed contradiction edge {right.id} -> {ctx.subject}", ctx.subject)
        for relation in snapshot.relations:
            if relation.predicate == "contradicts" and {relation.subject, relation.object} == pair_ids:
                return _result(
                    name, "pass", f"typed contradicts relation between {_listed(pair_ids)}", ctx.subject
                )
        if (
            left is not None
            and right is not None
            and left.current == "yes"
            and right.current == "yes"
            and (_conflict_marker(left) or _conflict_marker(right))
        ):
            return _result(
                name,
                "pass",
                f"both sides current with a conflict marker ({_listed(pair_ids)})",
                ctx.subject,
            )
        for item in snapshot.items:
            if _conflict_marker(item) and pair_ids <= set(item.cites):
                return _result(
                    name,
                    "pass",
                    f"review-queue conflict entry {item.id} cites both sides",
                    ctx.subject,
                )
        return _result(
            name,
            "fail",
            f"no contradiction representation for {_listed(pair_ids)}: no typed edge, "
            "no conflict marker on co-current items, no review-queue entry",
            ctx.subject,
        )

    edges = [item.id for item in snapshot.items if item.contradicts]
    edges += [
        relation.subject for relation in snapshot.relations if relation.predicate == "contradicts"
    ]
    markers = [item.id for item in snapshot.items if _conflict_marker(item)]
    if edges or markers:
        return _result(
            name,
            "pass",
            f"contradiction representation present (edges: {_listed(set(edges)) or 'none'}; "
            f"markers: {_listed(set(markers)) or 'none'})",
            ctx.subject,
        )
    return _result(name, "fail", "no contradiction edge or conflict marker in state", ctx.subject)


def contradiction_not_flattened(ctx: AssertionContext) -> AssertionResult:
    """Neither side of a conflict is silently gone. CATASTROPHIC."""

    name = "contradiction_not_flattened"
    gated = _gate(ctx, name, "contradicts", "current")
    if gated is not None:
        return gated

    snapshot = ctx.snapshot
    if ctx.subject and ctx.counterpart:
        left, right = _pair(ctx)
        if left is None and right is None:
            return _result(
                name, "unsupported", "neither side of the declared pair is observable", ctx.subject
            )
        if left is None or right is None:
            missing = ctx.subject if left is None else ctx.counterpart
            survivor = right if left is None else left
            assert survivor is not None
            return _result(
                name,
                "fail",
                f"flattened: {missing} is absent while {survivor.id} remains "
                f"(current={survivor.current}) with no conflict record",
                ctx.subject,
            )
        if left.current == "yes" and right.current == "yes":
            return _result(
                name,
                "pass",
                f"both sides retrievable as current ({left.id}, {right.id})",
                ctx.subject,
            )
        edge = (
            right.id in left.contradicts
            or left.id in right.contradicts
            or any(
                relation.predicate == "contradicts"
                and {relation.subject, relation.object} == {left.id, right.id}
                for relation in snapshot.relations
            )
        )
        retired = [item for item in (left, right) if item.current == "no"]
        if retired and (retired[0].retired_reason or edge):
            return _result(
                name,
                "pass",
                f"{retired[0].id} retired with a recorded reason/edge; both sides retained",
                ctx.subject,
            )
        return _result(
            name,
            "fail",
            f"one side settled without a conflict record ({left.id} current={left.current}, "
            f"{right.id} current={right.current})",
            ctx.subject,
        )

    by_id = snapshot.items_by_id()
    edges: list[tuple[str, str]] = []
    for item in snapshot.items:
        edges.extend((item.id, target) for target in item.contradicts)
    edges.extend(
        (relation.subject, relation.object)
        for relation in snapshot.relations
        if relation.predicate == "contradicts"
    )
    if not edges:
        return _result(name, "unsupported", "no contradiction edge observed", ctx.subject)
    dangling = [f"{a} -> {b}" for a, b in edges if b not in by_id or a not in by_id]
    if dangling:
        return _result(
            name, "fail", f"contradiction edges with a vanished side: {_listed(dangling)}", ctx.subject
        )
    return _result(
        name, "pass", f"{len(edges)} contradiction edge(s) retain both sides", ctx.subject
    )


# --------------------------------------------------------------------------
# 9-11: typing, questions, uncertainty
# --------------------------------------------------------------------------


def decision_distinguishable_from_hypothesis(ctx: AssertionContext) -> AssertionResult:
    """A fresh agent can tell a settled decision from a live hypothesis.

    Accepts a type/kind field, a documented collection convention visible in
    the locator, or a schema/metadata attribute.
    """

    name = "decision_distinguishable_from_hypothesis"
    gated = _gate(ctx, name, "kind")
    if gated is not None:
        return gated

    # Both members are co-equal items under comparison, so an unresolvable one
    # is unobservable rather than an invitation to scan the whole snapshot for
    # some other decision/hypothesis pair and answer about that instead.
    unobservable = _declared_but_unobservable(ctx, name, check_counterpart=True)
    if unobservable is not None:
        return unobservable

    snapshot = ctx.snapshot
    left, right = _pair(ctx)
    if left is None or right is None:
        decisions = [item for item in snapshot.items if item.kind == "decision"]
        hypotheses = [item for item in snapshot.items if item.kind == "hypothesis"]
        if decisions and hypotheses:
            return _result(
                name,
                "pass",
                f"typed kinds separate {decisions[0].id} (decision) from "
                f"{hypotheses[0].id} (hypothesis)",
                ctx.subject,
            )
        return _result(
            name,
            "unsupported",
            "no decision/hypothesis pair observable for the comparison",
            ctx.subject,
        )

    if left.kind != right.kind:
        return _result(
            name,
            "pass",
            f"typed kind field: {left.id}={left.kind} vs {right.id}={right.kind}",
            ctx.subject,
        )
    left_folder, right_folder = _folder(left.locator), _folder(right.locator)
    if left_folder and right_folder and left_folder != right_folder:
        left_decision_folder, left_hypothesis_folder = _collection_terms(left.locator)
        right_decision_folder, right_hypothesis_folder = _collection_terms(right.locator)
        if (left_decision_folder and right_hypothesis_folder) or (
            left_hypothesis_folder and right_decision_folder
        ):
            return _result(
                name,
                "pass",
                f"documented collection convention names the distinction: "
                f"{left_folder} vs {right_folder}",
                ctx.subject,
            )
    # A schema/metadata attribute only distinguishes the two concepts if it
    # *states* them. Two values that merely differ ("active" vs "draft") tell a
    # fresh agent nothing about which one is a settled decision, so accepting
    # any difference turned this invariant into a diff detector.
    shared_keys = sorted(set(left.raw) & set(right.raw))
    for key in shared_keys:
        left_value, right_value = left.raw[key], right.raw[key]
        if left_value == right_value:
            continue
        left_decision = states_value("decision", left_value)
        left_hypothesis = states_value("hypothesis", left_value)
        right_decision = states_value("decision", right_value)
        right_hypothesis = states_value("hypothesis", right_value)
        if (left_decision and right_hypothesis) or (left_hypothesis and right_decision):
            return _result(
                name,
                "pass",
                f"metadata attribute {key!r} states the distinction: "
                f"{left_value!r} vs {right_value!r}",
                ctx.subject,
            )
    return _result(
        name,
        "fail",
        f"{left.id} and {right.id} are indistinguishable: same kind {left.kind!r}, "
        "no collection naming decision vs hypothesis, and no metadata attribute "
        "stating it",
        ctx.subject,
    )


def open_question_queryable(ctx: AssertionContext) -> AssertionResult:
    """Modeled ignorance is retrievable.

    Accepts a dedicated item kind, a queryable tag/attribute, or a task/review
    queue entry.
    """

    name = "open_question_queryable"
    gated = _gate(ctx, name, "open_question", "kind")
    if gated is not None:
        return gated

    snapshot = ctx.snapshot
    typed = [item.id for item in snapshot.items if item.kind == "open_question"]
    if typed:
        return _result(name, "pass", f"dedicated open_question item(s): {_listed(typed)}", ctx.subject)
    for item in snapshot.items:
        for key, value in sorted(item.raw.items()):
            if states_value("open-question", value) or states_value("open_question", value):
                return _result(
                    name,
                    "pass",
                    f"queryable attribute {item.id}.{key} states an open question",
                    ctx.subject,
                )
    queued = [item.id for item in snapshot.items if _is_open(item.review_state)]
    if queued:
        return _result(name, "pass", f"review-queue entries open for inspection: {_listed(queued)}", ctx.subject)
    return _result(
        name,
        "fail",
        "no open question is queryable: no dedicated kind, no attribute, no queue entry",
        ctx.subject,
    )


def uncertainty_declared(ctx: AssertionContext) -> AssertionResult:
    """A conclusion whose support is thin says so."""

    name = "uncertainty_declared"
    gated = _gate(ctx, name, "uncertainty")
    if gated is not None:
        return gated

    snapshot = ctx.snapshot
    subjects = _belief_items(ctx)
    if ctx.subject and not subjects:
        candidate = snapshot.item(ctx.subject)
        subjects = (candidate,) if candidate is not None else ()
    if not subjects:
        return _result(name, "unsupported", "no conclusion observed to carry uncertainty", ctx.subject)

    missing: list[str] = []
    for item in subjects:
        if item.uncertainty and item.uncertainty.strip():
            continue
        if _is_open(item.review_state) or _is_uncertain(item.review_state):
            continue
        if any(
            question.kind == "open_question" and item.id in question.cites
            for question in snapshot.items
        ):
            continue
        missing.append(item.id)
    if missing:
        return _result(
            name, "fail", f"conclusions with no declared uncertainty: {_listed(missing)}", ctx.subject
        )
    return _result(
        name, "pass", f"{len(subjects)} conclusion(s) declare their uncertainty", ctx.subject
    )


# --------------------------------------------------------------------------
# 12-14: review lifecycle (snapshot pairs)
# --------------------------------------------------------------------------


def _require_pair(ctx: AssertionContext, name: str) -> AssertionResult | None:
    if ctx.prior is None:
        return _result(
            name, "unsupported", "this invariant needs a snapshot pair; none supplied", ctx.subject
        )
    return None


def review_state_durable(ctx: AssertionContext) -> AssertionResult:
    """A recorded review decision survives the transition."""

    name = "review_state_durable"
    gated = _gate(ctx, name, "review_state")
    if gated is not None:
        return gated
    missing_pair = _require_pair(ctx, name)
    if missing_pair is not None:
        return missing_pair
    assert ctx.prior is not None

    prior_by = ctx.prior.items_by_id()
    post_by = ctx.snapshot.items_by_id()
    tracked = [item for item in prior_by.values() if item.review_state]
    if ctx.subject:
        tracked = [item for item in tracked if item.id == ctx.subject]
    if not tracked:
        return _result(name, "unsupported", "no review state observed before the transition", ctx.subject)

    lost: list[str] = []
    for item in tracked:
        after = post_by.get(item.id)
        if after is None:
            lost.append(f"{item.id} absent after the transition")
        elif not after.review_state:
            lost.append(f"{item.id} lost review state {item.review_state!r}")
    if lost:
        return _result(name, "fail", f"review decisions did not survive: {_listed(lost)}", ctx.subject)
    return _result(
        name, "pass", f"{len(tracked)} review decision(s) survived the transition", ctx.subject
    )


def review_reopens_on_material_change(ctx: AssertionContext) -> AssertionResult:
    """A closed item whose content materially changed comes back open."""

    name = "review_reopens_on_material_change"
    gated = _gate(ctx, name, "review_state")
    if gated is not None:
        return gated
    missing_pair = _require_pair(ctx, name)
    if missing_pair is not None:
        return missing_pair
    assert ctx.prior is not None

    prior_by = ctx.prior.items_by_id()
    post_by = ctx.snapshot.items_by_id()
    candidates = []
    for item_id, before in prior_by.items():
        after = post_by.get(item_id)
        if after is None or not _is_closed(before.review_state):
            continue
        if _content_hash(before) != _content_hash(after):
            candidates.append((before, after))
    if ctx.subject:
        candidates = [pair for pair in candidates if pair[0].id == ctx.subject]
    if not candidates:
        return _result(
            name,
            "unsupported",
            "no closed item underwent a material change between the snapshots",
            ctx.subject,
        )

    offenders = [
        f"{after.id} stayed {after.review_state!r}"
        for _before, after in candidates
        if not _is_open(after.review_state)
    ]
    if offenders:
        return _result(
            name, "fail", f"material change did not reopen review: {_listed(offenders)}", ctx.subject
        )
    return _result(
        name,
        "pass",
        f"{len(candidates)} closed item(s) reopened after a material change",
        ctx.subject,
    )


def review_stays_closed_on_irrelevant_change(ctx: AssertionContext) -> AssertionResult:
    """A closed decision is not churned back open by an unrelated change."""

    name = "review_stays_closed_on_irrelevant_change"
    gated = _gate(ctx, name, "review_state")
    if gated is not None:
        return gated
    missing_pair = _require_pair(ctx, name)
    if missing_pair is not None:
        return missing_pair
    assert ctx.prior is not None

    prior_by = ctx.prior.items_by_id()
    post_by = ctx.snapshot.items_by_id()
    candidates = []
    for item_id, before in prior_by.items():
        after = post_by.get(item_id)
        if after is None or not _is_closed(before.review_state):
            continue
        if _content_hash(before) == _content_hash(after):
            candidates.append((before, after))
    if ctx.subject:
        candidates = [pair for pair in candidates if pair[0].id == ctx.subject]
    if not candidates:
        return _result(
            name,
            "unsupported",
            "no closed item survived unchanged across the snapshots",
            ctx.subject,
        )

    offenders = [
        f"{after.id} became {after.review_state!r}"
        for _before, after in candidates
        if not _is_closed(after.review_state)
    ]
    if offenders:
        return _result(
            name,
            "fail",
            f"closed review reopened without a material change: {_listed(offenders)}",
            ctx.subject,
        )
    return _result(
        name,
        "pass",
        f"{len(candidates)} closed decision(s) stayed closed through an irrelevant change",
        ctx.subject,
    )


# --------------------------------------------------------------------------
# 15-17: out-of-band operations (snapshot pairs)
# --------------------------------------------------------------------------


def external_edit_authoritative_within(ctx: AssertionContext) -> AssertionResult:
    """An out-of-band edit to the canonical artifact wins, inside the declared
    freshness bound. CATASTROPHIC."""

    name = "external_edit_authoritative_within"
    gated = _gate(ctx, name, "external_edit", "locator")
    if gated is not None:
        return gated
    missing_pair = _require_pair(ctx, name)
    if missing_pair is not None:
        return missing_pair
    assert ctx.prior is not None

    if ctx.freshness_bound_s is None:
        return _result(
            name,
            "unsupported",
            "no freshness bound declared; adoption latency cannot be scored",
            ctx.subject,
        )
    if ctx.external_edit_at is None:
        return _result(
            name, "unsupported", "no external-edit timestamp supplied by the scenario", ctx.subject
        )

    prior_by = ctx.prior.items_by_id()
    post_by = ctx.snapshot.items_by_id()
    target_ids = [ctx.subject] if ctx.subject else sorted(prior_by)
    adopted: list[str] = []
    ignored: list[str] = []
    for item_id in target_ids:
        before = prior_by.get(item_id)
        after = post_by.get(item_id)
        if before is None:
            continue
        if after is None:
            ignored.append(f"{item_id} vanished after the external edit")
        elif _content_hash(before) == _content_hash(after):
            ignored.append(f"{item_id} unchanged in observed state")
        else:
            adopted.append(item_id)
    if not adopted:
        return _result(
            name,
            "fail",
            f"authoritative external edit not reflected: {_listed(ignored) or 'no target observed'}",
            ctx.subject,
        )

    elapsed = _elapsed_seconds(ctx.external_edit_at, ctx.snapshot.taken_at)
    if elapsed is None:
        return _result(
            name, "blocked", "external-edit or snapshot timestamp is not parsable", ctx.subject
        )
    if elapsed <= ctx.freshness_bound_s:
        return _result(
            name,
            "pass",
            f"external edit to {_listed(adopted)} adopted in {elapsed:g}s, within the "
            f"declared bound {ctx.freshness_bound_s}s",
            ctx.subject,
        )
    return _result(
        name,
        "fail",
        f"external edit to {_listed(adopted)} adopted after {elapsed:g}s, outside the "
        f"declared bound {ctx.freshness_bound_s}s",
        ctx.subject,
    )


def export_reconstructs_state(ctx: AssertionContext) -> AssertionResult:
    """The export reconstructs items, currency, lineage, and evidence edges.

    ``ctx.prior`` is the live snapshot; ``ctx.snapshot`` is the export-derived
    one. Reconstruction loss above ``ctx.tolerance`` fails.
    """

    name = "export_reconstructs_state"
    gated = _gate(ctx, name, "export")
    if gated is not None:
        return gated
    missing_pair = _require_pair(ctx, name)
    if missing_pair is not None:
        return missing_pair
    assert ctx.prior is not None

    live = ctx.prior
    derived_by = ctx.snapshot.items_by_id()
    if not live.items:
        return _result(name, "unsupported", "live snapshot has no items to reconstruct", ctx.subject)

    mismatches: list[str] = []
    for item in live.items:
        found = derived_by.get(item.id)
        if found is None:
            mismatches.append(f"{item.id}: missing from export")
        elif found.kind != item.kind:
            mismatches.append(f"{item.id}: kind {found.kind} != {item.kind}")
        elif found.current != item.current:
            mismatches.append(f"{item.id}: currency {found.current} != {item.current}")
        elif found.revision_of != item.revision_of:
            mismatches.append(f"{item.id}: lineage {found.revision_of} != {item.revision_of}")
        elif set(_evidence_targets(live, item)) != set(
            _evidence_targets(ctx.snapshot, found)
        ):
            mismatches.append(f"{item.id}: evidence edges differ")
        elif _outgoing_edges(live, item.id) != _outgoing_edges(ctx.snapshot, item.id):
            # Typed relations are state. An export that keeps every item but
            # drops the graph has not reconstructed the state, and comparing
            # ``cites`` alone could not see that.
            mismatches.append(f"{item.id}: typed relations differ")
    loss = len(mismatches) / len(live.items)
    if loss <= ctx.tolerance:
        return _result(
            name,
            "pass",
            f"export reconstructs {len(live.items) - len(mismatches)}/{len(live.items)} "
            f"items (loss {loss:g} <= tolerance {ctx.tolerance:g})",
            ctx.subject,
        )
    return _result(
        name,
        "fail",
        f"export loss {loss:g} exceeds tolerance {ctx.tolerance:g}: {_listed(mismatches)}",
        ctx.subject,
    )


def dependent_conclusions_surfaced_for_review(ctx: AssertionContext) -> AssertionResult:
    """When support is retired, what depended on it is surfaced, not rewritten.

    A dependent that was quietly *rewritten* to match the new state is the
    failure this invariant exists to catch, not an excuse for it. An earlier
    build treated any content change as evidence the system had handled the
    retirement, which meant the most harmful behaviour — silently editing a
    conclusion the user still believes they authored — scored a pass. Only an
    open review flag (or the dependent being explicitly retired itself) counts
    as surfacing it.
    """

    name = "dependent_conclusions_surfaced_for_review"
    gated = _gate(ctx, name, "review_state", "cites")
    if gated is not None:
        return gated
    missing_pair = _require_pair(ctx, name)
    if missing_pair is not None:
        return missing_pair
    assert ctx.prior is not None

    prior_by = ctx.prior.items_by_id()
    post_by = ctx.snapshot.items_by_id()
    retired = {
        item_id
        for item_id, before in prior_by.items()
        if before.current != "no"
        and (post_by.get(item_id) is None or post_by[item_id].current == "no")
    }
    if not retired:
        return _result(
            name, "unsupported", "no support was retired between the snapshots", ctx.subject
        )

    offenders: list[str] = []
    dependents: list[str] = []
    for item in ctx.snapshot.items:
        targets = set(_evidence_targets(ctx.snapshot, item))
        if not targets & retired:
            continue
        dependents.append(item.id)
        before = prior_by.get(item.id)
        revised = before is not None and _content_hash(before) != _content_hash(item)
        if _is_open(item.review_state) or item.current == "no":
            continue
        if revised:
            offenders.append(f"{item.id} silently rewritten with no review flag")
        else:
            offenders.append(f"{item.id} still current, unflagged, unchanged")
    if not dependents:
        return _result(
            name, "unsupported", "no dependent conclusion cites the retired support", ctx.subject
        )
    if offenders:
        return _result(
            name,
            "fail",
            f"dependents silently unaffected by retired support: {_listed(offenders)}",
            ctx.subject,
        )
    return _result(
        name,
        "pass",
        f"{len(dependents)} dependent conclusion(s) surfaced for review after retirement",
        ctx.subject,
    )


# --------------------------------------------------------------------------
# 18: isolation
# --------------------------------------------------------------------------


def no_cross_case_residue(ctx: AssertionContext) -> AssertionResult:
    """No foreign-case canary is reachable from this case. CATASTROPHIC."""

    name = "no_cross_case_residue"
    if ctx.foreign_case_hits is None:
        return _result(
            name, "unsupported", "no cross-case canary probe was executed for this run", ctx.subject
        )
    if ctx.foreign_case_hits:
        return _result(
            name,
            "fail",
            f"foreign-case canary reachable: {_listed(ctx.foreign_case_hits)}",
            ctx.subject,
        )
    return _result(name, "pass", "cross-case canary probe returned no foreign hits", ctx.subject)


# --------------------------------------------------------------------------
# 19-24: loop closure (PREREGISTRATION §7, 2026-08 amendment; families f15-f19)
#
# These six are pre-registered ahead of the primitives they measure, which is
# the contracts-first point of the amendment: the bar is committed to before
# the product can clear it. Each follows the same discipline as the original
# eighteen — capability gate first, §4 alternates tried in order, the winning
# representation named in the evidence, and absence typed rather than zeroed.
# --------------------------------------------------------------------------


def due_prediction_surfaced(ctx: AssertionContext) -> AssertionResult:
    """An overdue prediction is surfaced through the documented interface.

    Accepts a deadline/status query over prediction units, or a due/review
    queue derived from dated prediction artifacts.
    """

    name = "due_prediction_surfaced"
    gated = _gate(ctx, name, "prediction", "review_state")
    if gated is not None:
        return gated
    unobservable = _declared_but_unobservable(ctx, name)
    if unobservable is not None:
        return unobservable

    snapshot = ctx.snapshot
    predictions = _prediction_items(snapshot)
    if ctx.subject:
        predictions = tuple(item for item in predictions if item.id == ctx.subject)
    if not predictions:
        return _result(
            name,
            "unsupported",
            "no prediction unit observed; a projector that records none cannot "
            "be asked whether an overdue one surfaces",
            ctx.subject,
        )

    served = set(ctx.served_items) if ctx.served_items is not None else None
    missed: list[str] = []
    for item in predictions:
        overdue = _raw_states(item, DUE_RAW_KEYS, DUE_STATES)
        queued = _is_open(item.review_state)
        if not overdue and not queued:
            missed.append(item.id)
            continue
        if served is not None and item.id not in served:
            missed.append(item.id)
            continue
        how = (
            f"deadline/status attribute states {overdue!r}"
            if overdue
            else f"due/review queue entry ({item.review_state})"
        )
        return _result(name, "pass", f"{item.id}: {how}", ctx.subject)
    return _result(
        name,
        "fail",
        f"no overdue prediction is surfaced: {_listed(missed)} state no elapsed "
        "window and sit in no queue",
        ctx.subject,
    )


def verdict_state_retrievable(ctx: AssertionContext) -> AssertionResult:
    """A resolved hypothesis or prediction can be asked what it resolved to.

    Accepts verdict metadata on the item, or a linked outcome artifact that
    carries the verdict and resolves back to what it adjudicates.
    """

    name = "verdict_state_retrievable"
    gated = _gate(ctx, name, "verdict", "kind", "cites")
    if gated is not None:
        return gated
    unobservable = _declared_but_unobservable(ctx, name)
    if unobservable is not None:
        return unobservable

    snapshot = ctx.snapshot
    subjects = _hypothesis_items(snapshot)
    if ctx.subject:
        subjects = tuple(item for item in subjects if item.id == ctx.subject)
    if not subjects:
        return _result(
            name,
            "unsupported",
            "no hypothesis or prediction observed to carry a verdict",
            ctx.subject,
        )

    missing: list[str] = []
    for item in subjects:
        verdict = _verdict_of(snapshot, item)
        if verdict is None:
            missing.append(item.id)
            continue
        value, how = verdict
        return _result(name, "pass", f"{item.id} verdict {value!r} via {how}", ctx.subject)
    return _result(
        name,
        "fail",
        f"no verdict is retrievable for {_listed(missing)}: no verdict attribute "
        "and no linked outcome that resolves back",
        ctx.subject,
    )


def divergence_surfaced_without_mutation(ctx: AssertionContext) -> AssertionResult:
    """Records that drift from a bound plan are surfaced, and nothing rewrites the plan.

    Both halves are load-bearing. Surfacing without the no-mutation check would
    pass a system that silently edited the plan to match reality, which is the
    failure the family exists to catch.
    """

    name = "divergence_surfaced_without_mutation"
    gated = _gate(ctx, name, "plan_linkage", "review_state")
    if gated is not None:
        return gated
    paired = _require_pair(ctx, name)
    if paired is not None:
        return paired
    unobservable = _declared_but_unobservable(ctx, name)
    if unobservable is not None:
        return unobservable

    snapshot = ctx.snapshot
    prior = ctx.prior
    assert prior is not None  # _require_pair
    plans = tuple(
        item for item in snapshot.items if _raw_value(item, PLAN_RAW_KEYS) is not None
    )
    if ctx.subject:
        plans = tuple(item for item in plans if item.id == ctx.subject)
    if not plans:
        return _result(
            name,
            "unsupported",
            "no plan artifact observed; a divergence from nothing is not measurable",
            ctx.subject,
        )

    mutated: list[str] = []
    unsurfaced: list[str] = []
    for plan in plans:
        before = prior.item(plan.id)
        if before is not None and _content_hash(before) != _content_hash(plan):
            mutated.append(plan.id)
            continue
        flagged = [
            item.id
            for item in snapshot.items
            if _normalize_state(item.review_state) in DIVERGENCE_STATES
            or _raw_states(item, PLAN_RAW_KEYS, DIVERGENCE_STATES) is not None
        ]
        if flagged:
            return _result(
                name,
                "pass",
                f"divergence surfaced for review ({_listed(flagged)}) while plan "
                f"{plan.id} is byte-identical across the transition",
                ctx.subject,
            )
        related = [
            relation.object
            for relation in snapshot.relations
            if relation.subject == plan.id
            and relation.predicate in {"contradicts", "relates_to"}
        ]
        if related:
            return _result(
                name,
                "pass",
                f"documented comparison surfaces {plan.id} against {_listed(related)} "
                "while the plan is unchanged across the transition",
                ctx.subject,
            )
        unsurfaced.append(plan.id)
    if mutated:
        return _result(
            name,
            "fail",
            f"plan auto-mutated to match the records: {_listed(mutated)} changed "
            "across the transition rather than being surfaced for review",
            ctx.subject,
        )
    return _result(
        name,
        "fail",
        f"records diverge but nothing surfaces it for {_listed(unsurfaced)}: "
        "no review entry and no documented comparison",
        ctx.subject,
    )


def support_collapse_inspectable(ctx: AssertionContext) -> AssertionResult:
    """Two derived notes resting on one source do not read as independent support.

    Accepts a provenance graph exposing the shared root, or documented source
    references that let both support paths be resolved to the same source.
    """

    name = "support_collapse_inspectable"
    gated = _gate(ctx, name, "cites", "kind")
    if gated is not None:
        return gated
    unobservable = _declared_but_unobservable(ctx, name)
    if unobservable is not None:
        return unobservable

    snapshot = ctx.snapshot
    consumers = tuple(
        item
        for item in snapshot.items
        if len(_evidence_targets(snapshot, item)) > 1 and item.kind in BELIEF_KINDS
    )
    if ctx.subject:
        consumers = tuple(item for item in consumers if item.id == ctx.subject)
    if not consumers:
        return _result(
            name,
            "unsupported",
            "no item rests on two or more supports; support collapse is not "
            "observable here",
            ctx.subject,
        )

    opaque: list[str] = []
    for consumer in consumers:
        supports = _evidence_targets(snapshot, consumer)
        roots_by_support = {
            support: _support_roots(snapshot, snapshot.item(support))
            for support in supports
            if snapshot.item(support) is not None
        }
        if len(roots_by_support) < 2 or any(not roots for roots in roots_by_support.values()):
            opaque.append(consumer.id)
            continue
        shared = frozenset.intersection(*roots_by_support.values())
        if shared:
            return _result(
                name,
                "pass",
                f"{consumer.id}: both support paths resolve to shared source "
                f"{_listed(sorted(shared))}, so the collapse is inspectable",
                ctx.subject,
            )
        return _result(
            name,
            "pass",
            f"{consumer.id}: support paths resolve to distinct sources "
            f"({_listed(sorted({r for rs in roots_by_support.values() for r in rs}))}); "
            "independence is inspectable rather than assumed",
            ctx.subject,
        )
    return _result(
        name,
        "fail",
        f"support structure is not inspectable for {_listed(opaque)}: at least one "
        "support path does not resolve to a source the snapshot exposes",
        ctx.subject,
    )


def refuted_retrievable_at_full_standing(ctx: AssertionContext) -> AssertionResult:
    """A refuted hypothesis stays retrievable, and is not demoted for being refuted.

    Distinguishable from active-unresolved (it carries a verdict) AND from
    superseded (it was not retired or replaced). Losing a negative result is the
    harm; quietly filing it as superseded is the same harm with better manners.
    """

    name = "refuted_retrievable_at_full_standing"
    gated = _gate(ctx, name, "verdict", "current", "kind")
    if gated is not None:
        return gated
    unobservable = _declared_but_unobservable(ctx, name)
    if unobservable is not None:
        return unobservable

    snapshot = ctx.snapshot
    candidates = _hypothesis_items(snapshot)
    if ctx.subject:
        candidates = tuple(item for item in candidates if item.id == ctx.subject)
    refuted = tuple(
        item
        for item in candidates
        if (_raw_states(item, VERDICT_RAW_KEYS, REFUTED_STATES) is not None)
        or (
            (verdict := _verdict_of(snapshot, item)) is not None and verdict[0] in REFUTED_STATES
        )
    )
    if not refuted:
        return _result(
            name,
            "unsupported",
            "no refuted hypothesis observed; the retention invariant has no subject",
            ctx.subject,
        )

    superseded = _superseded_ids(snapshot)
    served = set(ctx.served_items) if ctx.served_items is not None else None
    demoted: list[str] = []
    for item in refuted:
        reasons: list[str] = []
        if item.current == "no":
            reasons.append("currency withdrawn")
        if item.retired_reason:
            reasons.append(f"retired as {item.retired_reason!r}")
        if item.id in superseded or item.revision_of:
            reasons.append("filed as superseded")
        if served is not None and item.id not in served:
            reasons.append("not served by the documented retrieval surface")
        if reasons:
            demoted.append(f"{item.id} ({'; '.join(reasons)})")
            continue
        return _result(
            name,
            "pass",
            f"{item.id} is retrievable at full standing: verdict distinguishes it "
            "from active-unresolved, and it is neither retired nor superseded",
            ctx.subject,
        )
    return _result(
        name,
        "fail",
        f"refuted result demoted rather than retained: {_listed(demoted)}",
        ctx.subject,
    )


def loop_journey_state_coherent(ctx: AssertionContext) -> AssertionResult:
    """A full goal-to-revision journey survives a restart with its links intact.

    Scores the system rather than retrieval: the deterministic snapshots must
    show at least three sessions, the review outcome, and the resulting
    revision, and every stage observed before the restart must still be there
    afterwards, unchanged.
    """

    name = "loop_journey_state_coherent"
    gated = _gate(ctx, name, "kind", "cites", "verdict")
    if gated is not None:
        return gated
    paired = _require_pair(ctx, name)
    if paired is not None:
        return paired

    snapshot = ctx.snapshot
    prior = ctx.prior
    assert prior is not None  # _require_pair
    staged = {
        item.id: stage
        for item in snapshot.items
        if (stage := _raw_states(item, STAGE_RAW_KEYS, frozenset(JOURNEY_STAGES))) is not None
    }
    if not staged:
        return _result(
            name,
            "unsupported",
            "no journey stage observed; the composite family needs staged artifacts",
            ctx.subject,
        )

    sessions = {
        found[1]
        for item in snapshot.items
        if (found := _raw_value(item, SESSION_RAW_KEYS)) is not None
    }
    observed = set(staged.values())
    missing = [stage for stage in JOURNEY_STAGES if stage not in observed]

    lost: list[str] = []
    for item_id in sorted(staged):
        before = prior.item(item_id)
        current = snapshot.item(item_id)
        if before is not None and current is not None and _content_hash(before) != _content_hash(current):
            lost.append(f"{item_id} (rewritten across the restart)")
    for before in prior.items:
        stage = _raw_states(before, STAGE_RAW_KEYS, frozenset(JOURNEY_STAGES))
        if stage is not None and snapshot.item(before.id) is None:
            lost.append(f"{before.id} (stage {stage!r} lost across the restart)")
    if lost:
        return _result(
            name, "fail", f"journey did not survive the restart: {_listed(sorted(lost))}", ctx.subject
        )
    if missing:
        return _result(
            name,
            "fail",
            f"journey is incomplete: no artifact records stage(s) {_listed(missing)}",
            ctx.subject,
        )
    if len(sessions) < 3:
        return _result(
            name,
            "fail",
            f"journey spans {len(sessions)} session(s); the family requires at least three",
            ctx.subject,
        )
    unlinked = [
        item_id
        for item_id in sorted(staged)
        if not _evidence_targets(snapshot, snapshot.item(item_id))  # type: ignore[arg-type]
        and not _outgoing_edges(snapshot, item_id)
        and staged[item_id] != JOURNEY_STAGES[0]
    ]
    if unlinked:
        return _result(
            name,
            "fail",
            f"journey stages are not linked: {_listed(unlinked)} reference nothing upstream",
            ctx.subject,
        )
    return _result(
        name,
        "pass",
        f"all {len(JOURNEY_STAGES)} stages recorded across {len(sessions)} sessions, "
        "linked, and unchanged across the restart",
        ctx.subject,
    )


# --------------------------------------------------------------------------
# 20-26: the no-nudge families (amendment sequence 2)
#
# f20-f22 are expected to be *red* on the current runtime, and the quiet halves
# blocked, because the detectors, consumers and carriers they measure do not
# exist yet. That is the contract: these are falsification targets filed before
# the machinery, never tests tuned to pass.
# --------------------------------------------------------------------------


def _raw_int(item: StateItem, keys: frozenset[str]) -> int | None:
    """A non-negative integer recorded under a documented attribute, or ``None``."""

    found = _raw_value(item, keys)
    if found is None:
        return None
    try:
        parsed = int(found[1].strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normalized(value: str) -> str:
    return value.strip().casefold().replace("-", "_")


def _signal_targets(item: StateItem) -> tuple[str, ...]:
    """Item ids a signal is about, from its documented target attribute."""

    found = _raw_value(item, TARGET_RAW_KEYS)
    if found is None:
        return ()
    return tuple(part.strip() for part in found[1].replace(";", ",").split(",") if part.strip())


def _signal_surface(item: StateItem) -> str | None:
    found = _raw_value(item, SURFACE_RAW_KEYS)
    return _normalized(found[1]) if found is not None else None


def _surface_projection(snapshot: EpistemicStateSnapshot, surface: str) -> str | None:
    """How completely ``surface`` was projected, or ``None`` when it is missing.

    A *surface marker* carries both a surface name and a projection status; a
    signal sitting on the surface carries a signal class instead. Keeping the
    two apart is what stops a queue that happens to hold one entry from being
    read as proof that the queue was projected at all.
    """

    for item in snapshot.items:
        if _signal_surface(item) != _normalized(surface):
            continue
        projection = _raw_value(item, PROJECTION_RAW_KEYS)
        if projection is not None:
            return _normalized(projection[1])
    return None


def _signals_targeting(
    snapshot: EpistemicStateSnapshot,
    subject: str,
    vocabulary: frozenset[str],
) -> tuple[tuple[StateItem, str], ...]:
    """``(signal item, class token)`` for every signal of ``vocabulary`` about ``subject``."""

    found: list[tuple[StateItem, str]] = []
    for item in snapshot.items:
        token = _raw_states(item, SIGNAL_CLASS_RAW_KEYS, vocabulary)
        if token is None or subject not in _signal_targets(item):
            continue
        found.append((item, token))
    return tuple(found)


def _packet_item(snapshot: EpistemicStateSnapshot) -> StateItem | None:
    """The continuation packet, identified by its documented attribute."""

    for item in snapshot.items:
        if _raw_value(item, PACKET_RAW_KEYS) is not None:
            return item
    return None


def _packet_referenced_ids(
    snapshot: EpistemicStateSnapshot, packet: StateItem
) -> frozenset[str]:
    """Unit ids the packet holds **by reference**, never by inlined copy."""

    return frozenset(packet.cites) | frozenset(
        relation.object
        for relation in snapshot.relations
        if relation.subject == packet.id and relation.predicate in EVIDENCE_PREDICATES
    )


def _is_decoy(item: StateItem) -> bool:
    found = _raw_value(item, DECOY_RAW_KEYS)
    return found is not None and _normalized(found[1]) in {"yes", "true", "1"}


@claims_absence()
def signal_absence_checked_across_all_surfaces(
    ctx: AssertionContext, *, on_behalf_of: str | None = None
) -> AssertionResult:
    """The anti-vacuity meta-predicate: silence proven on every declared surface.

    Three cheats this exists to turn into structural failures. A product that
    stops emitting a queue item for a twin but still names it in the due-state
    counters block has *relocated* the nag, not removed it — so the counters
    block is one of the surfaces absence must be proven on. A projector that
    silently returns nothing for a surface has proven nothing at all — so an
    empty or missing projection is an error, never a pass. And a product that
    renames its nag into a signal class this predicate was not watching has
    changed nothing the user experiences — so the vocabulary is declared per
    caller and per family, and only ever widened.

    ``on_behalf_of`` is the composing predicate's own name, which is how a
    family declares the classes it is answerable for. Every negative control in
    f20-f26 composes this, which is what makes the family's false-positive
    ceiling worth asserting at all.
    """

    name = "signal_absence_checked_across_all_surfaces"
    vocabulary = ABSENCE_CLAIM_CLASSES[on_behalf_of or name] | FAMILY_ABSENCE_CLASSES.get(
        ctx.family or "", frozenset()
    )
    gated = _gate(ctx, name, "signal", "review_state", "due_state_counters")
    if gated is not None:
        return gated

    subject = ctx.subject
    if not subject:
        return _result(
            name, "blocked", "quiet assertion requires the subject it proves silence about", None
        )

    surfaces = ctx.absence_surfaces
    unprojected = [
        f"{surface}({_surface_projection(ctx.snapshot, surface) or 'missing'})"
        for surface in surfaces
        if _surface_projection(ctx.snapshot, surface) != PROJECTION_COMPLETE
    ]
    if unprojected:
        return _result(
            name,
            "blocked",
            f"absence cannot be established for {subject}: {_listed(unprojected)} "
            "did not project completely; an unprojected surface is an error, never silence",
            subject,
        )

    offenders = [
        f"{item.id} ({token} on {_signal_surface(item) or 'unnamed surface'})"
        for item, token in _signals_targeting(ctx.snapshot, subject, vocabulary)
    ]
    if offenders:
        return _result(
            name,
            "fail",
            f"{subject} is named by unsolicited signal(s) despite the quiet "
            f"expectation: {_listed(offenders)}",
            subject,
        )
    return _result(
        name,
        "pass",
        f"no signal in the {len(vocabulary)}-class vocabulary names {subject} on any of the "
        f"{len(surfaces)} completely projected surfaces ({', '.join(surfaces)})",
        subject,
    )


def structural_signal_surfaced_within_budget(ctx: AssertionContext) -> AssertionResult:
    """f20: accumulated structure surfaces a promotion-class signal, in budget.

    Binds to structure, never to vocabulary: the discriminator is the count of
    structurally distinct durable-unit clusters the note carries, and the budget
    comes from the frozen constants module so no fixture can retune it.
    """

    name = "structural_signal_surfaced_within_budget"
    gated = _gate(ctx, name, "signal", "review_state")
    if gated is not None:
        return gated

    subject = ctx.subject
    if not subject:
        return _result(name, "blocked", "f20 requires the accumulating note as subject", None)
    item = ctx.snapshot.item(subject)
    if item is None:
        return _result(name, "unsupported", f"{subject} is not present in the snapshot", subject)

    clusters = _raw_int(item, CLUSTER_COUNT_RAW_KEYS)
    if clusters is None:
        return _result(
            name,
            "unsupported",
            f"{subject} records no cluster count; the structural budget cannot be measured",
            subject,
        )

    open_signals = [
        (signal, token)
        for signal, token in _signals_targeting(ctx.snapshot, subject, PROMOTION_SIGNAL_CLASSES)
        if not _is_closed(signal.review_state)
    ]
    if not open_signals:
        return _result(
            name,
            "fail",
            f"{subject} accumulated {clusters} structurally distinct cluster(s) and no "
            "promotion-class signal is present in any open view",
            subject,
        )
    if clusters > STRUCTURAL_EMERGENCE_CLUSTER_BUDGET:
        return _result(
            name,
            "fail",
            f"{subject} surfaced only after {clusters} clusters, past the frozen budget of "
            f"{STRUCTURAL_EMERGENCE_CLUSTER_BUDGET}",
            subject,
        )
    signal, token = open_signals[0]
    return _result(
        name,
        "pass",
        f"{signal.id} ({token}) surfaced for {subject} at {clusters} cluster(s), within the "
        f"frozen budget of {STRUCTURAL_EMERGENCE_CLUSTER_BUDGET}",
        subject,
    )


def entity_candidate_surfaced_from_recurrence(ctx: AssertionContext) -> AssertionResult:
    """f21: an identity recurring with reusable facts surfaces as a candidate.

    The discriminator is *distinct sources carrying reusable facts*, not string
    frequency — which is exactly what the frequency-matched incidental-mention
    twin exists to prove, since a mutant that counted occurrences would
    otherwise pass both halves of the family.
    """

    name = "entity_candidate_surfaced_from_recurrence"
    gated = _gate(ctx, name, "signal", "review_state")
    if gated is not None:
        return gated

    subject = ctx.subject
    if not subject:
        return _result(name, "blocked", "f21 requires the recurring identity as subject", None)
    item = ctx.snapshot.item(subject)
    if item is None:
        return _result(name, "unsupported", f"{subject} is not present in the snapshot", subject)

    sources = _raw_int(item, SOURCE_COUNT_RAW_KEYS)
    if sources is None:
        return _result(
            name,
            "unsupported",
            f"{subject} records no distinct-source count; recurrence cannot be measured",
            subject,
        )

    surfaced = [
        (signal, token)
        for signal, token in _signals_targeting(ctx.snapshot, subject, ENTITY_SIGNAL_CLASSES)
        if not _is_closed(signal.review_state)
    ]
    if not surfaced:
        return _result(
            name,
            "fail",
            f"{subject} recurs across {sources} distinct source(s) and no entity-candidate "
            "signal is present in any open view",
            subject,
        )
    if sources < ENTITY_EMERGENCE_SOURCE_BUDGET:
        return _result(
            name,
            "fail",
            f"{subject} surfaced on only {sources} distinct source(s); the frozen threshold is "
            f"{ENTITY_EMERGENCE_SOURCE_BUDGET}, so this is a premature candidate",
            subject,
        )
    signal, token = surfaced[0]
    return _result(
        name,
        "pass",
        f"{signal.id} ({token}) surfaced for {subject} across {sources} distinct source(s)",
        subject,
    )


def contradiction_surfaced_unprompted(ctx: AssertionContext) -> AssertionResult:
    """f22: invalidating evidence surfaces the pair without the user asking.

    Unpromptedness is a *trajectory* property the loader enforces (zero topical
    agent turns between evidence ingest and the asserted snapshot), so what is
    left to assert here is that the pair actually surfaced. The surfacing route
    is reported in the evidence string and never asserted: which queue found it
    is a fact worth recording, not a property a product must satisfy one way.
    """

    name = "contradiction_surfaced_unprompted"
    gated = _gate(ctx, name, "signal", "contradicts", "review_state")
    if gated is not None:
        return gated

    subject, counterpart = ctx.subject, ctx.counterpart
    if not subject or not counterpart:
        return _result(
            name,
            "blocked",
            "f22 requires both the conclusion and the invalidating evidence",
            subject,
        )

    for signal, token in _signals_targeting(ctx.snapshot, subject, CONTRADICTION_SIGNAL_CLASSES):
        if counterpart not in _signal_targets(signal) or _is_closed(signal.review_state):
            continue
        route = _signal_surface(signal) or "unnamed surface"
        return _result(
            name,
            "pass",
            f"{signal.id} ({token}) surfaces {subject} against {counterpart}; "
            f"surfacing route reported: {route}",
            subject,
        )
    return _result(
        name,
        "fail",
        f"no open contradiction-class signal pairs {subject} with {counterpart}",
        subject,
    )


@claims_absence(ALL_SIGNAL_CLASSES)
def dismissal_respected_across_passes(ctx: AssertionContext) -> AssertionResult:
    """f23: a dismissed fingerprint stays dismissed until something material changes.

    The two halves are inseparable. Respecting a dismissal *forever* would be a
    bug, not a feature, so a changed fingerprint reopening the item is a pass;
    an unchanged fingerprint reappearing is the failure. Where nothing reappears
    at all, the meta-predicate is composed rather than trusted, so a product
    cannot pass by relocating the item to a surface nobody checked.

    The reappearance match is over **every** signal class with no subset
    anywhere in the path. Re-raising a dismissed fingerprint as a contradiction,
    a merge or a conflict is the same nag wearing a different hat, and the user
    experiencing it cannot tell the difference.
    """

    name = "dismissal_respected_across_passes"
    gated = _gate(ctx, name, "dismissal", "review_state", "signal")
    if gated is not None:
        return gated

    subject = ctx.subject
    if not subject:
        return _result(name, "blocked", "f23 requires the dismissed item as subject", None)
    prior = ctx.prior
    if prior is None:
        return _result(name, "blocked", "f23 compares a dismissal against a later pass", subject)

    dismissed = prior.item(subject)
    if dismissed is None:
        return _result(
            name, "unsupported", f"{subject} is absent from the earlier snapshot", subject
        )
    recorded = _raw_value(dismissed, FINGERPRINT_RAW_KEYS)
    if recorded is None or not _is_closed(dismissed.review_state):
        return _result(
            name,
            "unsupported",
            f"{subject} carries no closed triage decision with a fingerprint to respect",
            subject,
        )
    dismissed_fingerprint = _normalized(recorded[1])

    unchanged: list[str] = []
    for signal, token in _signals_targeting(
        ctx.snapshot, subject, ABSENCE_CLAIM_CLASSES["dismissal_respected_across_passes"]
    ):
        if _is_closed(signal.review_state):
            continue
        found = _raw_value(signal, FINGERPRINT_RAW_KEYS)
        if found is not None and _normalized(found[1]) != dismissed_fingerprint:
            return _result(
                name,
                "pass",
                f"{signal.id} ({token}) reopened {subject} under a changed fingerprint "
                f"{found[1]!r}; a material change is supposed to reopen",
                subject,
            )
        unchanged.append(f"{signal.id} ({token})")
    if unchanged:
        return _result(
            name,
            "fail",
            f"{subject} reappeared under the dismissed fingerprint {recorded[1]!r}: "
            f"{_listed(unchanged)}",
            subject,
        )

    absence = signal_absence_checked_across_all_surfaces(
        ctx, on_behalf_of="dismissal_respected_across_passes"
    )
    if absence.outcome != "pass":
        return _result(
            name,
            absence.outcome,
            f"dismissal of {subject} cannot be confirmed: {absence.evidence}",
            subject,
        )
    passes = _raw_int(ctx.snapshot.item(subject) or dismissed, PASS_COUNT_RAW_KEYS)
    survived = f" across {passes} maintenance pass(es)" if passes is not None else ""
    return _result(
        name, "pass", f"{subject} stayed dismissed{survived}; {absence.evidence}", subject
    )


def _counters_block(snapshot: Any) -> Any:
    """The due-state counters item on one snapshot, or ``None``."""
    if snapshot is None:
        return None
    for item in snapshot.items:
        if (
            _signal_surface(item) == "due_state_counters"
            and _raw_value(item, EMISSION_COUNT_RAW_KEYS) is not None
        ):
            return item
    return None


def counter_emission_not_repeated_per_write(ctx: AssertionContext) -> AssertionResult:
    """f23: a bulk batch does not emit one identical counters block per write.

    Counter-repetition is nagging under another name — the user asked for one
    batch and received N notifications — so the governance is asserted here
    rather than left to product taste.

    **Scored on the DELTA between the two snapshots, not on the totals.**
    `writes` and `emissions` are cumulative counters over the vault's whole
    life, so a ratio taken from the later snapshot alone answers a question
    about every batch that vault ever ran, not about the one under test. The
    denominator that makes the verdict about THIS batch is
    `later - prior`, and the assertion already receives both snapshots.

    An earlier version used the projection's `due_total` as the anti-vacuity
    guard. That field is the size of the LAST DELIVERED block and it persists,
    so once any earlier delivery had happened it stayed positive forever — and
    a later batch that delivered nothing scored `pass` on the strength of one
    block emitted before it started. `due_total` stays in the projection as
    what it is (informational: how big the last delivered block was) and no
    longer gates anything.
    """

    name = "counter_emission_not_repeated_per_write"
    gated = _gate(ctx, name, "due_state_counters", "signal")
    if gated is not None:
        return gated

    block = _counters_block(ctx.snapshot)
    if block is None:
        return _result(
            name,
            "unsupported",
            "no due-state counters block records an emission count",
            ctx.subject,
        )
    emissions = _raw_int(block, EMISSION_COUNT_RAW_KEYS)
    writes = _raw_int(block, WRITE_COUNT_RAW_KEYS)
    if emissions is None or writes is None:
        return _result(
            name,
            "unsupported",
            f"{block.id} does not record both an emission count and a write count",
            ctx.subject,
        )

    # The baseline. A run with no prior snapshot is measured from zero, which is
    # correct for a vault that starts empty and is the only honest reading when
    # there is nothing earlier to subtract.
    before = _counters_block(ctx.prior)
    prior_writes = _raw_int(before, WRITE_COUNT_RAW_KEYS) if before is not None else None
    prior_emissions = (
        _raw_int(before, EMISSION_COUNT_RAW_KEYS) if before is not None else None
    )
    writes_delta = writes - (prior_writes or 0)
    emissions_delta = emissions - (prior_emissions or 0)

    if writes_delta < 0 or emissions_delta < 0:
        return _result(
            name,
            "unsupported",
            f"{block.id} counters went backwards between the snapshots "
            f"(writes {prior_writes}->{writes}, emissions {prior_emissions}->{emissions}); "
            "the pair does not describe one batch",
            ctx.subject,
        )
    if writes_delta < 2:
        return _result(
            name,
            "unsupported",
            f"{block.id} records {writes_delta} governed write(s) between the snapshots; "
            "counter repetition needs a bulk batch",
            ctx.subject,
        )
    if emissions_delta == 0:
        # Anti-vacuity. A batch that delivered NOTHING had nothing that could
        # have repeated, so its lack of repetition is not evidence of anything.
        return _result(
            name,
            "unsupported",
            f"{block.id} delivered 0 block(s) across {writes_delta} governed write(s): "
            "nothing was delivered, so nothing could have repeated and the absence of "
            "repetition decides nothing",
            ctx.subject,
        )
    if emissions_delta >= writes_delta:
        return _result(
            name,
            "fail",
            f"{block.id} emitted {emissions_delta} counters block(s) for {writes_delta} "
            "write(s): one identical block per write is counter-repetition",
            ctx.subject,
        )
    return _result(
        name,
        "pass",
        f"{block.id} emitted {emissions_delta} counters block(s) for a batch of "
        f"{writes_delta} write(s)",
        ctx.subject,
    )


def continuation_packet_reconstructs_session(ctx: AssertionContext) -> AssertionResult:
    """f24: the packet holds every seeded unit by reference, and no decoy.

    Containment is *by reference*: a packet that inlines content is a copy, and
    a copy is precisely how a continuation aid drifts away from the state it
    claims to reconstruct.
    """

    name = "continuation_packet_reconstructs_session"
    gated = _gate(ctx, name, "continuation_packet", "cites")
    if gated is not None:
        return gated

    packet = _packet_item(ctx.snapshot)
    if packet is None:
        return _result(
            name, "fail", "no continuation packet is present in the snapshot", ctx.subject
        )
    referenced = _packet_referenced_ids(ctx.snapshot, packet)

    required = tuple(
        item
        for item in ctx.snapshot.items
        if item.id != packet.id
        and not _is_decoy(item)
        and (item.kind in {"decision", "open_question"} or _raw_value(item, PLAN_RAW_KEYS))
    )
    missing = [item.id for item in required if item.id not in referenced]
    if missing:
        return _result(
            name, "fail", f"{packet.id} omits seeded unit(s): {_listed(missing)}", ctx.subject
        )

    decoys = [item.id for item in ctx.snapshot.items if _is_decoy(item) and item.id in referenced]
    if decoys:
        return _result(
            name,
            "fail",
            f"{packet.id} admits foreign-project decoy(s): {_listed(decoys)}",
            ctx.subject,
        )
    if len(referenced) > CONTINUATION_PACKET_UNIT_BUDGET:
        return _result(
            name,
            "fail",
            f"{packet.id} references {len(referenced)} unit(s), past the frozen size budget of "
            f"{CONTINUATION_PACKET_UNIT_BUDGET}",
            ctx.subject,
        )
    return _result(
        name,
        "pass",
        f"{packet.id} references all {len(required)} seeded unit(s), excludes every decoy, and "
        f"holds {len(referenced)} unit(s) within the budget of {CONTINUATION_PACKET_UNIT_BUDGET}",
        ctx.subject,
    )


@claims_absence(MERGE_SIGNAL_CLASSES)
def restructure_signal_cleared_by_state_change(ctx: AssertionContext) -> AssertionResult:
    """f25: an applied restructure clears its own signal, without a dismissal.

    Clearing by dismissal would be the product hiding its own suggestion rather
    than the state change resolving it, so a dismissal recorded for the subject
    fails here even though the signal is gone. The second half forbids churn:
    nothing may propose folding the new children back together inside the frozen
    quiet window.
    """

    name = "restructure_signal_cleared_by_state_change"
    gated = _gate(ctx, name, "signal", "review_state")
    if gated is not None:
        return gated

    subject = ctx.subject
    if not subject:
        return _result(name, "blocked", "f25 requires the restructured note as subject", None)

    item = ctx.snapshot.item(subject)
    if (
        item is not None
        and _raw_value(item, FINGERPRINT_RAW_KEYS) is not None
        and _is_closed(item.review_state)
    ):
        return _result(
            name,
            "fail",
            f"{subject}'s structural signal is held by a dismissal ({item.review_state}), not "
            "cleared by the applied restructure",
            subject,
        )

    absence = signal_absence_checked_across_all_surfaces(
        ctx, on_behalf_of="restructure_signal_cleared_by_state_change"
    )
    if absence.outcome != "pass":
        return _result(
            name,
            absence.outcome,
            f"the restructured signal for {subject} is not demonstrably cleared: "
            f"{absence.evidence}",
            subject,
        )

    children = tuple(
        candidate
        for candidate in ctx.snapshot.items
        if (found := _raw_value(candidate, RESTRUCTURE_CHILD_RAW_KEYS)) is not None
        and (_normalized(found[1]) in {"yes", "true", "1"} or found[1].strip() == subject)
    )
    churn: list[str] = []
    for child in children:
        for signal, token in _signals_targeting(ctx.snapshot, child.id, MERGE_SIGNAL_CLASSES):
            passes = _raw_int(signal, PASS_COUNT_RAW_KEYS)
            if passes is None or passes <= RESTRUCTURE_QUIET_WINDOW_PASSES:
                churn.append(f"{signal.id} ({token} -> {child.id})")
    if churn:
        return _result(
            name,
            "fail",
            f"merge-class churn targets the new children inside the frozen window of "
            f"{RESTRUCTURE_QUIET_WINDOW_PASSES} pass(es): {_listed(churn)}",
            subject,
        )
    return _result(
        name,
        "pass",
        f"{subject}'s signal is absent from every open view with no dismissal recorded, and "
        f"{len(children)} new child(ren) drew no merge-class proposal inside the frozen window",
        subject,
    )


def due_state_block_present_in_carrier(ctx: AssertionContext) -> AssertionResult:
    """f26: the due-state block reaches a thin client's compact responses.

    Every other family measures a detector or an end state, so a runtime could
    satisfy all of them while no signal ever arrived anywhere a user could see
    it. This asserts the delivery path itself, against the compact responses the
    journey actually received.
    """

    name = "due_state_block_present_in_carrier"
    gated = _gate(ctx, name, "due_state_counters", "continuation_packet")
    if gated is not None:
        return gated

    responses = tuple(
        item
        for item in ctx.snapshot.items
        if (found := _raw_value(item, RESPONSE_DETAIL_RAW_KEYS)) is not None
        and _normalized(found[1]) == "compact"
    )
    if not responses:
        return _result(
            name,
            "unsupported",
            "no compact response was captured; the carrier path cannot be observed",
            ctx.subject,
        )

    projection = _surface_projection(ctx.snapshot, "due_state_counters")
    carried = [
        response.id
        for response in responses
        if "due_state_counters" in set(_signal_targets(response))
    ]
    # The two reasons a block can be missing are not the same finding, and
    # folding them together was hiding one of them. "The carrier delivered
    # nothing" is the family's negative result; "we never observed the surface"
    # is an error in the observation, and must not be reported as a product
    # failure. No shipped projector produces the blocked branch today
    # (journey_snapshot carries items only on a complete projection); it is a
    # defensive self-consistency check, exercised via hand-mutated snapshots
    # in the tests.
    if projection != PROJECTION_COMPLETE:
        if carried:
            return _result(
                name,
                "blocked",
                f"{_listed(carried)} reference a due-state block but the due_state_counters "
                f"surface did not project completely ({projection or 'missing'}); the "
                "observation contradicts itself and cannot settle delivery",
                ctx.subject,
            )
        return _result(
            name,
            "fail",
            f"none of the {len(responses)} compact response(s) carries a due-state block "
            f"({_listed(response.id for response in responses)}), and the due_state_counters "
            f"surface projected {projection or 'nothing'}: the block did not reach the client",
            ctx.subject,
        )
    if not carried:
        return _result(
            name,
            "fail",
            f"none of the {len(responses)} compact response(s) carries a due-state block: "
            f"{_listed(response.id for response in responses)}",
            ctx.subject,
        )
    return _result(
        name,
        "pass",
        f"{len(carried)} of {len(responses)} compact response(s) carry the due-state block: "
        f"{_listed(carried)}",
        ctx.subject,
    )


# --------------------------------------------------------------------------
# f27 lifecycle_routing_replay (2026-08 amendment, sequence 3)
# --------------------------------------------------------------------------

#: The two collection profiles the replay is measured over. Both must project,
#: because "the vault holds no plan items" and "we never observed Planning" are
#: different findings and only one of them is about the product.
REPLAY_PROFILES: tuple[str, ...] = ("planning", "records")

#: The three tiers, in reporting order. Counted separately and never summed: a
#: single number would let a product that files no intent look two-thirds right.
REPLAY_TIERS: tuple[str, ...] = ("intent", "outcome", "transition")


def _replay_collections(
    snapshot: EpistemicStateSnapshot,
) -> dict[str, "CollectionProjection"] | str:
    """``profile -> collection``, or the sentence saying why nothing can be read."""

    if not snapshot.collections:
        # The projector's own note travels with the refusal. A driver that
        # blocked an arm records why in the notes, and "nothing could be read"
        # is a far weaker finding than "nothing could be read because the agent
        # exited 1 before turn two".
        return (
            "the snapshot's collections section is empty; an unprojected section is "
            "an observation error, never a pass"
            + (f" ({snapshot.completeness_notes})" if snapshot.completeness_notes else "")
        )
    by_profile: dict[str, CollectionProjection] = {}
    for collection in snapshot.collections:
        by_profile.setdefault(_normalized(collection.profile), collection)
    missing = [profile for profile in REPLAY_PROFILES if profile not in by_profile]
    if missing:
        return (
            f"the snapshot's collections section carries no {_listed(missing)} profile "
            f"(saw {_listed({_normalized(c.profile) for c in snapshot.collections})}); "
            "an unprojected collection is an observation error, never a pass"
        )
    return by_profile


def _replay_expectation(subject: str | None):
    """The authored fold a scenario names, or the sentence saying why not."""

    from .corpora.lifecycle_replay import CorpusLookupError, expected_end_state

    try:
        return expected_end_state(subject or None)
    except CorpusLookupError as error:
        return str(error)


def _replay_normalized(value: str) -> str:
    from .corpora.lifecycle_replay import normalize

    return normalize(value)


def _valid_date(value: str) -> bool:
    try:
        datetime.strptime(value.strip(), "%Y-%m-%d")  # noqa: DTZ007 - a date, not a clock
    except (TypeError, ValueError):
        return False
    return True


def _plan_rows(collection: "CollectionProjection") -> tuple[tuple[str, str | None], ...]:
    """``(normalised title, status)`` for every item of the planning collection."""

    return tuple(
        (_replay_normalized(item.natural_key.get("title", "")), item.status)
        for item in collection.items
    )


def _record_rows(
    collection: "CollectionProjection",
) -> tuple[tuple[tuple[str, str], str], ...]:
    """``((normalised title, normalised event type), occurred_on)`` per record."""

    return tuple(
        (
            (
                _replay_normalized(item.natural_key.get("title", "")),
                _replay_normalized(item.natural_key.get("event_type", "")),
            ),
            item.natural_key.get("occurred_on", ""),
        )
        for item in collection.items
    )


def replay_coverage(
    snapshot: EpistemicStateSnapshot, corpus_id: str | None
) -> dict[str, dict[str, object]] | str:
    """Per-tier landed/expected counts, or the sentence saying why nothing can be read.

    The single mechanism behind both the coverage assertion and the run report:
    a report that recomputed the fractions its own way could disagree with the
    assertion it is printed beside, and the pair would stop being one reading of
    one run.
    """

    collections = _replay_collections(snapshot)
    if isinstance(collections, str):
        return collections
    expected = _replay_expectation(corpus_id)
    if isinstance(expected, str):
        return expected

    plan_status = dict(_plan_rows(collections["planning"]))
    record_rows = dict(_record_rows(collections["records"]))
    missing: dict[str, list[str]] = {tier: [] for tier in REPLAY_TIERS}

    for key in expected.plan_items:
        if key not in plan_status:
            missing["intent"].append(key)

    for key, record in expected.records.items():
        occurred_on = record_rows.get(key)
        if occurred_on is None:
            missing["outcome"].append(f"{key[0]}/{key[1]}")
        elif not _valid_date(occurred_on):
            missing["outcome"].append(f"{key[0]}/{key[1]} (occurred_on {occurred_on!r})")
        elif record.occurred_on and occurred_on.strip() != record.occurred_on:
            missing["outcome"].append(
                f"{key[0]}/{key[1]} (occurred_on {occurred_on!r}, corpus states "
                f"{record.occurred_on!r})"
            )

    for key, status in expected.transitions.items():
        observed = plan_status.get(key)
        if observed is None or _replay_normalized(observed) != _replay_normalized(status):
            missing["transition"].append(f"{key} -> {status} (observed {observed!r})")

    return {
        tier: {
            "expected": expected.tier_size(tier),
            "landed": expected.tier_size(tier) - len(missing[tier]),
            "missing": tuple(missing[tier]),
        }
        for tier in REPLAY_TIERS
    }


def _page_locators(snapshot: EpistemicStateSnapshot | None) -> frozenset[str]:
    """Every file page a snapshot projects.

    ``#`` locators are the open-thread pseudo-items the projector derives from a
    page's own headings, not files; surface markers carry no locator at all.
    """

    if snapshot is None:
        return frozenset()
    return frozenset(
        item.locator
        for item in snapshot.items
        if item.locator_kind == "file" and item.locator and "#" not in item.locator
    )


def replay_extras(
    snapshot: EpistemicStateSnapshot,
    corpus_id: str | None,
    seeded: EpistemicStateSnapshot | None,
) -> tuple[str, ...] | str:
    """Everything projected that the corpus fold does not account for.

    The page baseline is the **seeded snapshot** — what the vault held before
    turn 1, in this phase — and never a literal list. ``init_vault`` lays a
    scaffold whose contents are the product's to choose; a restated allowlist
    would either go stale against it or, worse, keep passing while the scaffold
    changed underneath. A page in the seed is a page the harness laid.

    ``seeded`` is checked, not trusted: same phase as the scored snapshot, and
    pre-turn in content. See the comment at the guard for why the runner cannot
    be the one to enforce that.
    """

    collections = _replay_collections(snapshot)
    if isinstance(collections, str):
        return collections
    expected = _replay_expectation(corpus_id)
    if isinstance(expected, str):
        return expected
    if seeded is None:
        return (
            "no seeded snapshot was observed before the replay, so a page cannot be "
            "told from one the harness itself laid; a missing baseline is an "
            "observation error, never a pass"
        )

    from .corpora.lifecycle_replay import (
        FILED_STATUS,
        SEEDED_COLLECTION_IDS,
        SEEDED_PLAN_TITLES,
    )

    seeded_titles = {_replay_normalized(title) for title in SEEDED_PLAN_TITLES}

    # The baseline has to be THIS phase's seed. `runner.evaluate_scenario`
    # reaches snapshots cumulatively across the whole scenario — deliberately,
    # other families take their pair across phases — so `ctx.prior` for the
    # second arm is the first arm's POST-RUN projection unless the second arm
    # took its own seed snapshot. Scored that way, a page both arms wrote is
    # already in the "baseline" and the dual passes on the exact write it exists
    # to catch. f27 refuses here rather than changing the runner.
    if seeded.phase != snapshot.phase:
        return (
            f"the snapshot offered as the baseline was taken in phase "
            f"{seeded.phase!r}, not {snapshot.phase!r}: phase {snapshot.phase!r} "
            "reached no seeded snapshot of its own, and another phase's vault is "
            "not a baseline for this one"
        )
    seeded_view = _replay_collections(seeded)
    if isinstance(seeded_view, str):
        return f"the snapshot offered as the baseline could not be read: {seeded_view}"
    baseline_records = [key for key, _occurred_on in _record_rows(seeded_view["records"])]
    baseline_extra_titles = sorted(
        title for title, _status in _plan_rows(seeded_view["planning"])
        if title not in seeded_titles
    )
    if baseline_records or baseline_extra_titles:
        return (
            "the snapshot offered as the baseline was not taken before turn 1: the "
            f"seeded vault holds no record and no plan item outside {sorted(seeded_titles)}, "
            f"and this one holds {len(baseline_records)} record(s) and "
            f"{len(baseline_extra_titles)} further plan item(s)"
        )

    extras: list[str] = []

    for title, status in _plan_rows(collections["planning"]):
        if title in seeded_titles:
            allowed = frozenset({FILED_STATUS})
        else:
            item = expected.plan_items.get(title)
            if item is None:
                extras.append(f"plan item {title!r}")
                continue
            allowed = item.assigned_statuses
        if status is not None and _replay_normalized(status) not in {
            _replay_normalized(candidate) for candidate in allowed
        }:
            extras.append(f"plan item {title!r} status {status!r}")

    for key, _occurred_on in _record_rows(collections["records"]):
        if key not in expected.records:
            extras.append(f"record {key[0]!r} ({key[1]})")

    for collection in snapshot.collections:
        if collection.id not in SEEDED_COLLECTION_IDS:
            extras.append(f"collection {collection.id!r} ({collection.manifest})")

    # Exactly the manifest file and the storage subdirectory that manifest
    # declares. The round-0 rule exempted a collection's whole parent directory,
    # which let a prose page written *instead of* a record — the corpus's own
    # stated failure case — sit inside `Planning/Delivery/` and pass.
    owned: set[str] = set()
    storage_prefixes: list[str] = []
    for collection in snapshot.collections:
        owned.add(collection.manifest)
        if "/" in collection.manifest and collection.storage_source:
            directory = collection.manifest.rsplit("/", 1)[0]
            storage_prefixes.append(f"{directory}/{collection.storage_source.strip('/')}/")

    baseline = _page_locators(seeded)
    for locator in sorted(_page_locators(snapshot)):
        if locator in baseline or locator in owned:
            continue
        if any(locator.startswith(prefix) for prefix in storage_prefixes):
            continue
        extras.append(f"page {locator!r}")

    return tuple(extras)


def lifecycle_consequence_landed_unprompted(ctx: AssertionContext) -> AssertionResult:
    """f27: every consequence an expert lands is in the replay's projected state.

    Three tiers, counted separately and never summed: a plan item filed from
    stated intent, a record appended from an observed outcome, and an open item
    whose status an outcome moved. It passes only when *every* tier is complete,
    because a family that averaged them would let a product that never files an
    intent look two-thirds right.

    The expectation comes from the corpus fold the scenario names in ``subject``.
    The transcript is never read: what an agent said it did is not what it did.
    """

    name = "lifecycle_consequence_landed_unprompted"
    coverage = replay_coverage(ctx.snapshot, ctx.subject)
    if isinstance(coverage, str):
        return _result(name, "blocked", coverage, ctx.subject)

    fractions = " · ".join(
        f"{tier} {coverage[tier]['landed']}/{coverage[tier]['expected']}"
        for tier in REPLAY_TIERS
    )
    incomplete = [tier for tier in REPLAY_TIERS if coverage[tier]["missing"]]
    if incomplete:
        detail = "; ".join(
            f"{tier} missing {_listed(coverage[tier]['missing'])}" for tier in incomplete
        )
        return _result(name, "fail", f"coverage {fractions}: {detail}", ctx.subject)
    return _result(
        name,
        "pass",
        f"coverage {fractions}: every consequence the corpus declares is present",
        ctx.subject,
    )


def no_structured_write_beyond_expectation(ctx: AssertionContext) -> AssertionResult:
    """f27: the false-write dual. Nothing was written that the fold did not expect.

    Coverage alone would reward a product that wrote a record for every sentence,
    so this is reported beside it from the same run and never on its own. Four
    kinds of extra: a plan item or a record outside the expected set, a plan item
    holding a status the fold never assigned, a collection beyond the seeded two,
    and a page that was neither in the seeded vault nor a file the collection's
    own manifest declares it stores.

    ``ctx.prior`` is the seeded vault's projection, taken before turn 1, in this
    phase. Without it the assertion blocks rather than guessing a baseline,
    because "the agent wrote this page" and "the scaffold shipped it" are
    different findings — and so are "this arm wrote it" and "the previous arm
    did", which is what a baseline reached from another phase would conflate.
    """

    name = "no_structured_write_beyond_expectation"
    extras = replay_extras(ctx.snapshot, ctx.subject, ctx.prior)
    if isinstance(extras, str):
        return _result(name, "blocked", extras, ctx.subject)
    if extras:
        return _result(
            name,
            "fail",
            f"{len(extras)} extra structured write(s) beyond the corpus fold: "
            f"{_listed(extras)}",
            ctx.subject,
        )
    return _result(
        name,
        "pass",
        "0 extra structured writes: every projected plan item, record, collection "
        "and page is one the corpus fold accounts for",
        ctx.subject,
    )
