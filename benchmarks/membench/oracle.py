"""Pure bitemporal truth oracle.

The single source of truth for expected answers: the generator derives
``expected.jsonl`` from these functions and every scorer re-derives through
them — no hand-written expectations, no inference. Templates author explicit
status spans; the oracle only evaluates (world time × knowledge week) and
lints that every span is justified by evidence.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from membench.clock import week_date
from membench.schema import (
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


def visible_spans(claim: ClaimRecord, knowledge_week: int) -> list[StatusSpan]:
    return [s for s in claim.status_timeline if s.recorded_week <= knowledge_week]


def _covering(spans: list[StatusSpan], world_t: date) -> list[StatusSpan]:
    out = []
    for span in spans:
        if span.valid_from <= world_t and (span.valid_to is None or world_t < span.valid_to):
            out.append(span)
    return out


def _evidence(claim: ClaimRecord, knowledge_week: int) -> tuple[tuple[str, ...], tuple[str, ...]]:
    supporting = tuple(
        a.source_id
        for a in claim.assertions
        if a.stance is Stance.SUPPORTS and a.recorded_week <= knowledge_week
    )
    disputing = tuple(
        a.source_id
        for a in claim.assertions
        if a.stance is Stance.DISPUTES and a.recorded_week <= knowledge_week
    )
    return supporting, disputing


def truth_at(claim: ClaimRecord, world_t: date, knowledge_week: int) -> TruthView:
    """What the corpus believed about ``world_t``, given everything recorded
    through ``knowledge_week``. Later-recorded spans win over earlier ones for
    the same world interval (retroactive corrections)."""

    supporting, disputing = _evidence(claim, knowledge_week)
    candidates = _covering(visible_spans(claim, knowledge_week), world_t)
    if not candidates:
        return TruthView(ClaimStatus.UNKNOWN, None, None, None, supporting, disputing)
    indexed = list(enumerate(candidates))
    _, span = max(indexed, key=lambda pair: (pair[1].recorded_week, pair[0]))
    superseded_by = (
        claim.superseded_by
        if span.status in (ClaimStatus.SUPERSEDED, ClaimStatus.PARTIALLY_SUPERSEDED)
        else None
    )
    value = claim.object if span.status is not ClaimStatus.UNKNOWN else None
    return TruthView(span.status, value, span, superseded_by, supporting, disputing)


def current_truth(claim: ClaimRecord, knowledge_week: int) -> TruthView:
    return truth_at(claim, world_cutoff(knowledge_week), knowledge_week)


def evolution(claim: ClaimRecord, knowledge_week: int) -> list[TruthView]:
    """Ordered belief history visible at ``knowledge_week`` (by world start)."""

    views: list[TruthView] = []
    for span in sorted(
        visible_spans(claim, knowledge_week), key=lambda s: (s.valid_from, s.recorded_week)
    ):
        view = truth_at(claim, span.valid_from, knowledge_week)
        if not views or views[-1].span is not view.span:
            views.append(view)
    return views


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
            if assertion.recorded_week <= knowledge_week:
                ordered.setdefault(assertion.source_id)
        for span in visible_spans(claim, knowledge_week):
            if span.cause.by is not None and is_source_id(span.cause.by):
                ordered.setdefault(span.cause.by)
        for reference in claim.derived_from:
            if not is_source_id(reference):
                continue
            source = (sources_by_id or {}).get(reference)
            if source is None or source.recorded_week <= knowledge_week:
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
