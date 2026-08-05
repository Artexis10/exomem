"""Pure bitemporal truth oracle.

The single source of truth for expected answers: the generator derives
``expected.jsonl`` from these functions and every scorer re-derives through
them — no hand-written expectations, no inference. Templates author explicit
status spans; the oracle only evaluates (world time × knowledge week) and
lints that every span is justified by evidence.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

from membench.clock import week_date
from membench.schema import (
    Assertion,
    ClaimRecord,
    ClaimStatus,
    EntityRecord,
    ExpectedRecord,
    PolicySet,
    SourceRecord,
    SpanCauseKind,
    Stance,
    StatusSpan,
    TypedValue,
)

#: Records carrying knowledge time. ``EventRecord`` is deliberately absent —
#: no rule in this module consults its ``recorded_week`` (see schema docstring).
RecordedRecord = Assertion | StatusSpan | SourceRecord


class Order(enum.Enum):
    """Result of comparing two records on the knowledge axis.

    ``INDETERMINATE`` is an answer, not a failure: two records sharing a week
    where either never captured an intra-day instant genuinely do not
    determine an order, and reporting that beats a coin flip. The vocabulary
    deliberately matches the product's ``src/exomem/temporal.Order``, but this
    module does **not** import it: the oracle is the ground truth a contender
    is measured against, so it may not be computed by the code under test — a
    bug shared with the implementation would be invisible to the benchmark by
    construction. Nothing under ``membench.schema``/``oracle``/``generate``
    imports ``exomem``; only the adapters that drive a product do.
    """

    BEFORE = "before"
    AFTER = "after"
    SAME = "same"
    INDETERMINATE = "indeterminate"

_ACTIVE_STATUSES = frozenset(
    {ClaimStatus.CURRENT, ClaimStatus.CONFIRMED, ClaimStatus.TENTATIVE, ClaimStatus.DISPUTED}
)

_STATUS_CAUSES: dict[ClaimStatus, frozenset[SpanCauseKind]] = {
    ClaimStatus.CURRENT: frozenset(
        {
            SpanCauseKind.INITIAL,
            SpanCauseKind.CONFIRMATION,
            SpanCauseKind.LATE_EVIDENCE,
            SpanCauseKind.CORRECTION,
        }
    ),
    ClaimStatus.TENTATIVE: frozenset({SpanCauseKind.INITIAL}),
    ClaimStatus.CONFIRMED: frozenset({SpanCauseKind.CONFIRMATION}),
    ClaimStatus.DISPROVED: frozenset({SpanCauseKind.DISPROOF}),
    ClaimStatus.REVOKED: frozenset({SpanCauseKind.RETRACTION}),
    ClaimStatus.SUPERSEDED: frozenset({SpanCauseKind.SUPERSESSION}),
    ClaimStatus.PARTIALLY_SUPERSEDED: frozenset({SpanCauseKind.SUPERSESSION}),
    ClaimStatus.DISPUTED: frozenset({SpanCauseKind.DISPUTE}),
    ClaimStatus.UNKNOWN: frozenset({SpanCauseKind.INITIAL, SpanCauseKind.EXPIRY}),
}


def world_cutoff(week: int) -> date:
    """World-time instant used for "as of week N" questions: end of that week."""

    return week_date(week, 6)


def is_claim_id(identifier: str) -> bool:
    return identifier.startswith("CLM-")


def is_source_id(identifier: str) -> bool:
    return identifier.startswith("SRC-")


@dataclass(frozen=True)
class TruthView:
    status: ClaimStatus
    value: TypedValue | None
    span: StatusSpan | None
    superseded_by: str | None
    supporting: tuple[str, ...] = ()
    disputing: tuple[str, ...] = ()
    #: True when two covering spans could not be ordered from their recorded
    #: time and authoring order picked the winner (see :func:`_resolve_span`).
    resolved_by_authoring_order: bool = False

    @property
    def is_active(self) -> bool:
        return self.status in _ACTIVE_STATUSES


@dataclass(frozen=True)
class ChangeView:
    before: TruthView
    after: TruthView
    changed: bool
    causes: tuple[str, ...]


@dataclass(frozen=True)
class Visibility:
    allowed: bool
    withhold_notice: bool
    rule_id: str | None
    tombstoned: bool = False


def compare_recorded(left: RecordedRecord, right: RecordedRecord) -> Order:
    """Order two records on the knowledge axis, four-valued.

    Weeks decide whenever they differ — a whole week precedes the next. Within
    one week the answer depends on what was actually captured: two instants
    order exactly, an instant against an uncaptured one does not (the unknown
    ranges over the entire week), and two uncaptured ones do not either. The
    unknown is never collapsed into a guess.
    """

    if left.recorded_week != right.recorded_week:
        return Order.BEFORE if left.recorded_week < right.recorded_week else Order.AFTER
    if left.recorded_offset_s is None or right.recorded_offset_s is None:
        return Order.INDETERMINATE
    if left.recorded_offset_s == right.recorded_offset_s:
        return Order.SAME
    return Order.BEFORE if left.recorded_offset_s < right.recorded_offset_s else Order.AFTER


def sort_key(record: RecordedRecord) -> tuple[int, int]:
    """Stable total order for listing; does **not** resolve indeterminacy.

    Uncaptured instants sort before captured ones inside their week, and ties
    fall to the caller's stable sort. Use :func:`compare_recorded` for any
    verdict; this exists only so histories can be listed reproducibly.
    """

    return (record.recorded_week, -1 if record.recorded_offset_s is None else record.recorded_offset_s)


def is_recorded_by(
    record: RecordedRecord, knowledge_week: int, knowledge_offset_s: int | None = None
) -> bool:
    """Is ``record`` determinately at or before this ask's knowledge cutoff?

    The single visibility predicate; every filter in this module goes through
    it. ``knowledge_offset_s=None`` — the only thing an
    :class:`~membench.schema.Ask` can express — puts the cutoff at the *end* of
    the knowledge week, which is what ``recorded_week <= knowledge_week`` has
    always meant: every record of that week lies inside it whatever its
    precision. So the finer axis refines the old rule rather than standing
    beside it, and no v0.1–v0.2 verdict moves.

    A sub-day cutoff is answered conservatively: a record whose intra-day
    instant was never captured is not *provably* at or before it, so it is not
    visible. Guessing the other way would let an ask see evidence the corpus
    cannot show it had.
    """

    if record.recorded_week != knowledge_week:
        return record.recorded_week < knowledge_week
    if knowledge_offset_s is None:
        return True
    if record.recorded_offset_s is None:
        return False
    return record.recorded_offset_s <= knowledge_offset_s


def _resolve_span(candidates: list[StatusSpan]) -> tuple[StatusSpan, bool]:
    """The latest-recorded covering span, and whether *authoring order* decided it.

    Later-recorded spans win over earlier ones. When :func:`compare_recorded`
    cannot separate two of them — same week, at least one without a captured
    instant, or the same instant exactly — there is nothing in the data to
    decide with, and the historical rule (the span declared last wins) is kept
    so no existing corpus changes verdict. That fallback is reported rather
    than hidden: the second element is ``True`` whenever it was used, and
    :func:`positional_resolutions` enumerates every claim that depends on it.
    A claim that captures any intra-day instant may not rely on it at all —
    :func:`lint_claim` refuses to generate one that does.
    """

    best = 0
    positional = False
    for index in range(1, len(candidates)):
        order = compare_recorded(candidates[index], candidates[best])
        if order is Order.AFTER:
            best, positional = index, False
        elif order in (Order.SAME, Order.INDETERMINATE):
            best, positional = index, True
    return candidates[best], positional


def visible_spans(
    claim: ClaimRecord, knowledge_week: int, *, knowledge_offset_s: int | None = None
) -> list[StatusSpan]:
    return [
        s for s in claim.status_timeline if is_recorded_by(s, knowledge_week, knowledge_offset_s)
    ]


def _overlap(left: StatusSpan, right: StatusSpan) -> bool:
    """Do two spans describe any common world instant?"""

    starts_before_other_ends = right.valid_to is None or left.valid_from < right.valid_to
    ends_after_other_starts = left.valid_to is None or right.valid_from < left.valid_to
    return starts_before_other_ends and ends_after_other_starts


def _covering(spans: list[StatusSpan], world_t: date) -> list[StatusSpan]:
    out = []
    for span in spans:
        if span.valid_from <= world_t and (span.valid_to is None or world_t < span.valid_to):
            out.append(span)
    return out


def _evidence(
    claim: ClaimRecord, knowledge_week: int, knowledge_offset_s: int | None = None
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    visible = [a for a in claim.assertions if is_recorded_by(a, knowledge_week, knowledge_offset_s)]
    supporting = tuple(a.source_id for a in visible if a.stance is Stance.SUPPORTS)
    disputing = tuple(a.source_id for a in visible if a.stance is Stance.DISPUTES)
    return supporting, disputing


def truth_at(
    claim: ClaimRecord,
    world_t: date,
    knowledge_week: int,
    *,
    knowledge_offset_s: int | None = None,
) -> TruthView:
    """What the corpus believed about ``world_t``, given everything recorded
    through the ask's knowledge cutoff. Later-recorded spans win over earlier
    ones for the same world interval (retroactive corrections); where recorded
    time cannot separate two of them, see :func:`_resolve_span`."""

    supporting, disputing = _evidence(claim, knowledge_week, knowledge_offset_s)
    candidates = _covering(
        visible_spans(claim, knowledge_week, knowledge_offset_s=knowledge_offset_s), world_t
    )
    if not candidates:
        return TruthView(ClaimStatus.UNKNOWN, None, None, None, supporting, disputing)
    span, positional = _resolve_span(candidates)
    superseded_by = (
        claim.superseded_by
        if span.status in (ClaimStatus.SUPERSEDED, ClaimStatus.PARTIALLY_SUPERSEDED)
        else None
    )
    value = claim.object if span.status is not ClaimStatus.UNKNOWN else None
    return TruthView(span.status, value, span, superseded_by, supporting, disputing, positional)


def current_truth(
    claim: ClaimRecord, knowledge_week: int, *, knowledge_offset_s: int | None = None
) -> TruthView:
    return truth_at(
        claim,
        world_cutoff(knowledge_week),
        knowledge_week,
        knowledge_offset_s=knowledge_offset_s,
    )


def evolution(claim: ClaimRecord, knowledge_week: int) -> list[TruthView]:
    """Ordered belief history visible at ``knowledge_week`` (by world start)."""

    views: list[TruthView] = []
    for span in sorted(
        visible_spans(claim, knowledge_week), key=lambda s: (s.valid_from, sort_key(s))
    ):
        view = truth_at(claim, span.valid_from, knowledge_week)
        if not views or views[-1].span is not view.span:
            views.append(view)
    return views


def learned_at(
    claim: ClaimRecord, *, knowledge_week: int, knowledge_offset_s: int | None = None
) -> Assertion | None:
    """The visible supporting assertion that first put this claim in the corpus.

    ``None`` when nothing supporting it is visible yet. Where several share the
    earliest knowledge time the first declared is returned; callers that need a
    verdict about *which claim came last* must use :func:`latest_recorded`,
    which refuses to break such a tie.
    """

    visible = [
        a
        for a in claim.assertions
        if a.stance is Stance.SUPPORTS and is_recorded_by(a, knowledge_week, knowledge_offset_s)
    ]
    if not visible:
        return None
    earliest = visible[0]
    for assertion in visible[1:]:
        if compare_recorded(assertion, earliest) is Order.BEFORE:
            earliest = assertion
    return earliest


def latest_recorded(
    claims: Sequence[ClaimRecord],
    *,
    knowledge_week: int,
    knowledge_offset_s: int | None = None,
) -> ClaimRecord | None:
    """The claim the corpus learned **last**, or ``None`` when it cannot tell.

    This is the whole point of sub-day knowledge time: which of several claims
    a memory system heard about most recently. ``None`` covers every case where
    the answer is not in the data — nothing visible, or a leader the recorded
    instants cannot separate from a rival (same week without captured instants,
    or the very same second). It is deliberately four-valued collapsed to
    "answer or no answer", with no authoring-order fallback: this function is
    new surface, so nothing depends on a guess, and an ask whose truth is
    indeterminate is one whose honest expected answer is abstention.
    """

    scored: list[tuple[ClaimRecord, Assertion]] = []
    for claim in claims:
        assertion = learned_at(
            claim, knowledge_week=knowledge_week, knowledge_offset_s=knowledge_offset_s
        )
        if assertion is not None:
            scored.append((claim, assertion))
    if not scored:
        return None
    leader, leader_at = scored[0]
    for claim, assertion in scored[1:]:
        if compare_recorded(assertion, leader_at) is Order.AFTER:
            leader, leader_at = claim, assertion
    for claim, assertion in scored:
        if claim is leader:
            continue
        if compare_recorded(assertion, leader_at) is not Order.BEFORE:
            return None
    return leader


def positional_resolutions(
    claims: Iterable[ClaimRecord], *, knowledge_week: int
) -> tuple[str, ...]:
    """Claim ids whose current truth is decided by authoring order, not data.

    A standing audit of the debt :func:`_resolve_span` carries. Every id listed
    here is a claim where two covering spans share a week and neither captured
    an intra-day instant, so which one is current depends on the order the rows
    happen to sit in ``claims.jsonl``. New corpora should not add to this list;
    claims that capture instants provably cannot (:func:`lint_claim`).
    """

    return tuple(
        claim.claim_id
        for claim in claims
        if current_truth(claim, knowledge_week).resolved_by_authoring_order
    )


def superseded_toward(
    claim_id: str,
    targets: Iterable[str],
    *,
    claims_by_id: dict[str, ClaimRecord],
    world_t: date,
    knowledge_week: int,
) -> tuple[str, ...]:
    """The documented supersession chain from ``claim_id`` to one of ``targets``.

    Returns the ordered successor claim ids walked, ending on the target that
    was reached, or ``()`` when the oracle cannot walk one.

    Every hop is taken from :func:`truth_at`, never from the raw
    ``superseded_by`` field, so it is **visible and superseded at the ask**:
    ``TruthView.superseded_by`` is populated only when the active span for that
    (world time × knowledge week) is ``superseded`` or ``partially_superseded``.
    A supersession recorded after the ask therefore does not count, which is
    the bitemporal separation the rest of the module keeps.

    The walk is transitive because corpora revise more than once: a reading
    corrected 197 -> 209 -> 217 leaves *both* earlier values superseded toward
    the current one, and a one-hop test would see only the last of them.
    """

    wanted = frozenset(targets)
    if not wanted:
        return ()
    path: list[str] = []
    seen: set[str] = set()
    current = claim_id
    while current in claims_by_id and current not in seen:
        seen.add(current)
        successor = truth_at(claims_by_id[current], world_t, knowledge_week).superseded_by
        if successor is None:
            return ()
        path.append(successor)
        if successor in wanted:
            return tuple(path)
        current = successor
    return ()


def required_citations(
    claim: ClaimRecord,
    view: TruthView,
    *,
    claims_by_id: dict[str, ClaimRecord] | None = None,
    knowledge_week: int,
    transitive: bool = True,
    _seen: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    """Source ids that must back an answer using this view.

    The justifying source of the active span comes first, then visible
    supporting assertions; ``derived_from`` chains expand transitively to
    original sources when ``transitive`` is set.
    """

    ordered: dict[str, None] = {}
    if view.span is not None and view.span.cause.by and is_source_id(view.span.cause.by):
        ordered.setdefault(view.span.cause.by)
    for source_id in view.supporting:
        ordered.setdefault(source_id)
    if transitive:
        for parent in claim.derived_from:
            if is_source_id(parent):
                ordered.setdefault(parent)
            elif claims_by_id and is_claim_id(parent) and parent not in _seen:
                parent_claim = claims_by_id.get(parent)
                if parent_claim is not None:
                    parent_view = current_truth(parent_claim, knowledge_week)
                    for source_id in required_citations(
                        parent_claim,
                        parent_view,
                        claims_by_id=claims_by_id,
                        knowledge_week=knowledge_week,
                        transitive=True,
                        _seen=_seen | {claim.claim_id},
                    ):
                        ordered.setdefault(source_id)
    return tuple(ordered)


def entity_names(entities_by_id: dict[str, EntityRecord]) -> dict[str, frozenset[str]]:
    """Every string that designates an entity, mapped to the entities it names.

    Canonical names, aliases and historical ``name_timeline`` names all count:
    a rename memo carrying the *old* name still resolves reference.
    """

    names: dict[str, set[str]] = {}
    for entity in entities_by_id.values():
        for name in (
            entity.canonical_name,
            *entity.aliases,
            *(span.name for span in entity.name_timeline),
        ):
            names.setdefault(name, set()).add(entity.entity_id)
    return {name: frozenset(ids) for name, ids in names.items()}


def claim_neighbourhood(
    basis: Iterable[str],
    claims_by_id: dict[str, ClaimRecord],
    *,
    entities_by_id: dict[str, EntityRecord] | None = None,
) -> frozenset[str]:
    """Claims an answer about ``basis`` is entitled to draw evidence from.

    Closed under three relations, to a fixpoint, because provenance for an
    answer is not confined to the claim that states it:

    - ``derived_from`` in **both** directions. Upward reaches the originals a
      derived claim rests on. Downward reaches a claim derived *from* the
      basis — a weekly digest restating the same reading is on-topic evidence,
      and a one-way walk would score citing it as unsupported.
    - supersession partners, which document the change under discussion.
    - **reference-resolving claims about an entity already in the closure.**

    The third edge is deliberately *not* "every claim about the same subject".
    That version admitted arbitrary predicates: a field report measuring a
    yield score became precise provenance for a question about a delivery
    date, because both happen to be about the same project. Shotgunning inside
    an entity was free, which is the original hole one level down.

    The edge exists so a system can prove the entity named in one source is
    the entity named in another, so it admits exactly the claims that do that
    work: those whose object *names* an entity already in the closure. A claim
    asserting ``official_name = "Project Driftreach"`` resolves reference and
    is admitted; ``yield-score = 25.1`` resolves nothing and is not. The test
    is structural — it asks whether the object value is an entity name, never
    what the predicate is called — so it does not rot as templates add
    predicates.

    Without ``entities_by_id`` no name can be resolved, so this edge is
    skipped entirely; callers must treat that as *unverifiable* rather than as
    a narrower answer (see :func:`permitted_citations`), because silently
    dropping the edge would fail honest multi-hop answers.
    """

    names = entity_names(entities_by_id or {})
    children: dict[str, list[str]] = {}
    by_subject: dict[str, list[str]] = {}
    for claim in claims_by_id.values():
        by_subject.setdefault(claim.subject, []).append(claim.claim_id)
        for parent in claim.derived_from:
            if is_claim_id(parent):
                children.setdefault(parent, []).append(claim.claim_id)

    seen: set[str] = set()
    frontier = [claim_id for claim_id in basis if claim_id in claims_by_id]
    while frontier:
        claim_id = frontier.pop()
        if claim_id in seen:
            continue
        seen.add(claim_id)
        claim = claims_by_id[claim_id]
        adjacent = [parent for parent in claim.derived_from if is_claim_id(parent)]
        adjacent.extend(children.get(claim_id, ()))
        adjacent.extend(
            rel for rel in (claim.supersedes, claim.superseded_by) if rel and is_claim_id(rel)
        )
        if names:
            known = {claims_by_id[other].subject for other in seen} | {claim.subject}
            for sibling_id in by_subject.get(claim.subject, ()):
                sibling = claims_by_id[sibling_id]
                designated = names.get(sibling.object.value)
                if designated and designated & known:
                    adjacent.append(sibling_id)
        frontier.extend(
            other for other in adjacent if other in claims_by_id and other not in seen
        )
    return frozenset(seen)


def evidence_neighbourhood(
    basis: Iterable[str],
    *,
    claims_by_id: dict[str, ClaimRecord],
    knowledge_week: int,
    entities_by_id: dict[str, EntityRecord] | None = None,
    sources_by_id: dict[str, SourceRecord] | None = None,
) -> tuple[str, ...]:
    """Source ids the oracle can tie to the evidence neighbourhood of ``basis``.

    For every claim in :func:`claim_neighbourhood` this admits assertions of
    **every** stance (a disputed claim's objector is real provenance, and the
    corpus asks for it via ``UncertaintyExpectation.cite_both_sides``), the
    justifying source of **every** visible status span rather than only the
    active one, and ``derived_from`` source references.

    Recorded-week visibility is honoured on all three: an assertion, span or
    referenced source recorded after ``knowledge_week`` is invisible and is
    *not* admitted, so a contender that cites evidence it could not yet have
    seen is outside the set. That filter is the bitemporal separation this
    benchmark rests on. A ``derived_from`` source absent from ``sources_by_id``
    is admitted, because its recorded week is then unknown and unprovable.

    This makes **no** claim about :func:`required_citations`. A template is free
    to require citations this walk does not reach; the superset guarantee lives
    in :func:`permitted_citations`, which is what scorers must use.
    """

    ordered: dict[str, None] = {}
    neighbourhood = claim_neighbourhood(
        basis, claims_by_id, entities_by_id=entities_by_id
    )
    for claim_id in sorted(neighbourhood):
        claim = claims_by_id[claim_id]
        for assertion in claim.assertions:
            if is_recorded_by(assertion, knowledge_week):
                ordered.setdefault(assertion.source_id)
        for span in visible_spans(claim, knowledge_week):
            if span.cause.by is not None and is_source_id(span.cause.by):
                ordered.setdefault(span.cause.by)
        for reference in claim.derived_from:
            if not is_source_id(reference):
                continue
            source = (sources_by_id or {}).get(reference)
            if source is None or is_recorded_by(source, knowledge_week):
                ordered.setdefault(reference)
    return tuple(ordered)


def permitted_citations(
    expected: ExpectedRecord,
    *,
    claims_by_id: dict[str, ClaimRecord],
    knowledge_week: int,
    entities_by_id: dict[str, EntityRecord] | None = None,
    sources_by_id: dict[str, SourceRecord] | None = None,
) -> tuple[frozenset[str], str | None]:
    """Citations an answer to ``expected`` may name without being provably wrong.

    Returns ``(permitted, unverifiable_reason)``. The reason is non-``None``
    when precision cannot be established: the record names no claims the oracle
    can resolve, or entity records are unavailable while the basis has sibling
    claims whose admission depends on resolving names. Per the module contract
    an unmeasurable verdict is reported as unsupported rather than decided
    either way — guessing narrow would fail honest multi-hop answers, guessing
    wide would wave through the shotgun.

    ``permitted`` always contains ``expected.required_citations``. That is a
    guarantee of *this* function, not an observed property of the graph walk: a
    template may require a citation :func:`evidence_neighbourhood` does not
    reach, and no caller should ever be able to punish an answer for citing
    exactly what the record demands.
    """

    basis = list(dict.fromkeys([*expected.required_claims, *expected.forbidden_claims]))
    if not basis:
        return frozenset(expected.required_citations), "expected record names no claims"
    unknown = [claim_id for claim_id in basis if claim_id not in claims_by_id]
    if unknown:
        return (
            frozenset(expected.required_citations),
            f"claims absent from the scoring index: {unknown}",
        )
    if not entities_by_id:
        subjects = {claims_by_id[claim_id].subject for claim_id in basis}
        siblings = [
            claim
            for claim in claims_by_id.values()
            if claim.subject in subjects and claim.claim_id not in basis
        ]
        if siblings:
            return (
                frozenset(expected.required_citations),
                "entity records unavailable to resolve same-entity references",
            )
    return (
        frozenset(expected.required_citations).union(
            evidence_neighbourhood(
                basis,
                claims_by_id=claims_by_id,
                knowledge_week=knowledge_week,
                entities_by_id=entities_by_id,
                sources_by_id=sources_by_id,
            )
        ),
        None,
    )


def what_changed(
    claim: ClaimRecord, world_t0: date, world_t1: date, knowledge_week: int
) -> ChangeView:
    before = truth_at(claim, world_t0, knowledge_week)
    after = truth_at(claim, world_t1, knowledge_week)
    changed = before.span is not after.span
    causes: tuple[str, ...] = ()
    if changed and after.span is not None and after.span.cause.by:
        causes = (after.span.cause.by,)
    return ChangeView(before, after, changed, causes)


def persona_audiences(policy: PolicySet, persona_id: str) -> frozenset[str]:
    for persona in policy.personas:
        if persona.persona_id == persona_id:
            return frozenset(persona.audiences)
    return frozenset()


def visibility(
    policy: PolicySet,
    persona_id: str,
    *,
    claim_id: str | None = None,
    source_id: str | None = None,
    item_audiences: list[str] | None = None,
    at: date,
) -> Visibility:
    """Deterministic disclosure decision: most-restrictive active rule wins;
    declassification opens a rule at its date; tombstones remove sources from
    all future release (forward-only)."""

    held = persona_audiences(policy, persona_id)
    if source_id is not None:
        for tomb in policy.tombstones:
            if source_id in tomb.target_sources and at >= tomb.requested_at:
                return Visibility(False, False, None, tombstoned=True)
    if item_audiences:
        if not held.intersection(item_audiences):
            return Visibility(False, True, None)
    for rule in policy.rules:
        targeted = (claim_id is not None and claim_id in rule.target_claims) or (
            source_id is not None and source_id in rule.target_sources
        )
        if not targeted:
            continue
        if rule.declassify_at is not None and at >= rule.declassify_at:
            continue  # declassified: rule no longer restricts
        if not held.intersection(rule.allow):
            return Visibility(False, rule.withhold_notice, rule.rule_id)
    return Visibility(True, False, None)


def lint_claim(claim: ClaimRecord, known_ids: frozenset[str]) -> list[str]:
    """Every span must be justified; unjustified corpora must not generate."""

    errors: list[str] = []
    cid = claim.claim_id
    assertion_sources = {a.source_id for a in claim.assertions}
    initial_spans = [s for s in claim.status_timeline if s.cause.kind is SpanCauseKind.INITIAL]
    if len(initial_spans) != 1:
        errors.append(f"{cid}: expected exactly one initial span, found {len(initial_spans)}")
    else:
        earliest = min(s.recorded_week for s in claim.status_timeline)
        if initial_spans[0].recorded_week != earliest:
            errors.append(f"{cid}: initial span must be the earliest recorded")
    if any(span.recorded_offset_s is not None for span in claim.status_timeline):
        # This claim captured intra-day instants, so its order must come from
        # them. Two spans the recorded time cannot separate would silently fall
        # back to authoring order (:func:`_resolve_span`) — a corpus asserting
        # a precision it does not have, and unaskable ground truth. Refuse.
        for i, left in enumerate(claim.status_timeline):
            for right in claim.status_timeline[i + 1 :]:
                order = compare_recorded(left, right)
                if order in (Order.SAME, Order.INDETERMINATE) and _overlap(left, right):
                    errors.append(
                        f"{cid}: spans declare sub-day knowledge time but are "
                        f"{order.value} with respect to each other while covering a "
                        "common world interval; which is current would be decided by "
                        "row order, not by data"
                    )
    for index, span in enumerate(claim.status_timeline):
        allowed = _STATUS_CAUSES.get(span.status, frozenset())
        if span.cause.kind not in allowed:
            errors.append(
                f"{cid}[{index}]: status {span.status.value} cannot be caused by "
                f"{span.cause.kind.value}"
            )
        if span.cause.by is None:
            errors.append(f"{cid}[{index}]: span has no justifying reference")
            continue
        if span.cause.by not in known_ids:
            errors.append(f"{cid}[{index}]: cause.by {span.cause.by} is not a known id")
        if is_source_id(span.cause.by) and span.cause.by not in assertion_sources:
            errors.append(
                f"{cid}[{index}]: justifying source {span.cause.by} missing from assertions"
            )
    superseded = any(
        s.status in (ClaimStatus.SUPERSEDED, ClaimStatus.PARTIALLY_SUPERSEDED)
        for s in claim.status_timeline
    )
    if superseded and not claim.superseded_by:
        errors.append(f"{cid}: superseded span present but superseded_by is unset")
    if claim.superseded_by and claim.superseded_by not in known_ids:
        errors.append(f"{cid}: superseded_by {claim.superseded_by} is not a known id")
    return errors


def lint_corpus(claims: list[ClaimRecord], sources: list[SourceRecord]) -> list[str]:
    known: set[str] = {s.source_id for s in sources}
    known.update(c.claim_id for c in claims)
    frozen = frozenset(known)
    errors: list[str] = []
    for claim in claims:
        errors.extend(lint_claim(claim, frozen))
    return errors


@dataclass
class CorpusIndex:
    """Convenience bundle handed to scorers."""

    claims_by_id: dict[str, ClaimRecord] = field(default_factory=dict)
    sources_by_id: dict[str, SourceRecord] = field(default_factory=dict)
