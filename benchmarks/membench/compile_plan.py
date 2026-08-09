"""Compile plan: the conclusions a real vault would hold, derived from the oracle.

Track B bulk-loads every corpus document as a raw source and compiles nothing,
so the structure `provenance` and `contradiction_uncertainty` score never comes
into existence — a citation chain is a compiled conclusion declaring its
sources, and contradiction detection runs over compiled conclusions that
disagree. Measured on the 2026-08-07 vault: 205 pages under `Sources/`, zero
compiled notes, `ingested_into: []` on 204 of 204, zero `derived_from`.

This module produces what a compiled altitude needs: one conclusion per claim,
carrying its cited sources and its lineage.

**No model is involved.** Every field is computed from records the oracle
already holds — the sources asserting a claim, the supersession chain, and
claims that assert an incompatible value. This is transduction of known ground
truth, which is in bounds under the project's pure-substrate constraint, and it
is deliberately *not* a judgment about what deserves remembering. A scripted
compile is more faithful than a bulk dump and less faithful than an agent
choosing what to keep; only the agent-in-the-loop tracks answer the latter.

Neutrality
==========

:class:`ConclusionRecord` carries only what any knowledge store has: a
conclusion, the sources it draws from, and its lineage. It must never grow a
field named after one product's API — a record shaped by product A hands A a
native fit and every other contender a translation layer, which is the
2026-08-05 renderer defect with the sign flipped in our own favour. The guard
is a test, and the ordering discipline is in the spec: the *second* product's
renderer is written first.
"""

from __future__ import annotations

from collections import defaultdict

from membench.ids import stable_id
from membench.schema import ClaimRecord, ConclusionRecord, EntityRecord, Stance


def _supporting_sources(claim: ClaimRecord) -> tuple[str, ...]:
    """Sources that assert the claim, in recorded order.

    Disputing and retracting assertions are excluded: a source that objects to a
    claim is evidence *about* it, not evidence *for* it. Citing objectors as
    basis would give every disputed conclusion support it never had, and would
    widen the permitted set the citation-precision gate checks against.
    """

    ordered: dict[str, None] = {}
    for assertion in claim.assertions:
        if assertion.stance is Stance.SUPPORTS:
            ordered.setdefault(assertion.source_id)
    return tuple(ordered)


def _supporting_by_week(claim: ClaimRecord) -> list[tuple[int, tuple[str, ...]]]:
    """Cumulative basis at each week the basis actually changed.

    Returns ``(knowledge_week, cites)`` ascending, where ``cites`` is every
    supporting source recorded at or before that week. Weeks at which nothing
    new arrived are skipped: the chain exists to record that the basis changed,
    so a basis that never changed must not manufacture a revision.
    """

    by_week: dict[int, list[str]] = defaultdict(list)
    for assertion in claim.assertions:
        if assertion.stance is Stance.SUPPORTS:
            by_week[assertion.recorded_week].append(assertion.source_id)

    revisions: list[tuple[int, tuple[str, ...]]] = []
    cumulative: dict[str, None] = {}
    for week in sorted(by_week):
        for source_id in by_week[week]:
            cumulative.setdefault(source_id)
        revisions.append((week, tuple(cumulative)))
    return revisions


def conclusion_id_for(claim_id: str, knowledge_week: int) -> str:
    """Stable, content-free id for one revision of one claim's conclusion.

    Keyed by week as well as claim (4b.39): revisions that shared an id would
    collapse wherever a plan is indexed by conclusion id, which is how every
    consumer reads it.
    """

    return stable_id("CON", f"{claim_id}@w{knowledge_week}")


def _disputing_pairs(claims: list[ClaimRecord]) -> dict[str, tuple[str, ...]]:
    """Claim ids that assert incompatible values for the same subject+predicate.

    The trap here is supersession. A superseded claim and its replacement share
    a subject, a predicate and differ in value — identical to a dispute on a
    naive read. Treating them as disputing would manufacture a conflict across
    the entire temporal family and make the contradiction dimension meaningless
    in the opposite direction from the one it fails in today.

    Two claims therefore dispute each other only when they differ in value AND
    neither supersedes the other, directly or transitively.
    """

    by_topic: dict[tuple[str, str], list[ClaimRecord]] = defaultdict(list)
    for claim in claims:
        by_topic[(claim.subject, claim.predicate)].append(claim)

    lineage: dict[str, set[str]] = defaultdict(set)
    for claim in claims:
        if claim.supersedes:
            lineage[claim.claim_id].add(claim.supersedes)
            lineage[claim.supersedes].add(claim.claim_id)
        if claim.superseded_by:
            lineage[claim.claim_id].add(claim.superseded_by)
            lineage[claim.superseded_by].add(claim.claim_id)

    def _related(a: str, b: str, seen: frozenset[str] = frozenset()) -> bool:
        if b in lineage[a]:
            return True
        for nxt in lineage[a] - seen - {a}:
            if _related(nxt, b, seen | {a}):
                return True
        return False

    disputes: dict[str, set[str]] = defaultdict(set)
    for group in by_topic.values():
        if len(group) < 2:
            continue
        for left in group:
            for right in group:
                if left.claim_id >= right.claim_id:
                    continue
                if left.object.value == right.object.value:
                    continue  # corroboration, not conflict
                if _related(left.claim_id, right.claim_id):
                    continue  # a lineage step, not a disagreement
                disputes[left.claim_id].add(right.claim_id)
                disputes[right.claim_id].add(left.claim_id)
    return {k: tuple(sorted(v)) for k, v in disputes.items()}


def _phrase(claim: ClaimRecord, names: dict[str, str]) -> str:
    """Readable subject-predicate phrase, using the entity's name when known."""

    subject = names.get(claim.subject, claim.subject)
    return f"{claim.predicate.replace('_', ' ')} of {subject}"


def derive_compile_plan(
    claims: list[ClaimRecord],
    entities: list[EntityRecord] | None = None,
) -> list[ConclusionRecord]:
    """Conclusions per claim revision in stable supersession order.

    Stability is load-bearing rather than tidy: the plan is written into the
    corpus and hashed into the release manifest, so an ordering that depended on
    input order would make generated bytes depend on template execution order.

    Each claim yields one conclusion per point at which its basis changed, not
    one conclusion outright (4b.39). A single atemporal conclusion cited every
    supporting source whatever week it was recorded, so a query asking as of
    knowledge week 3 was served a conclusion resting on evidence that did not
    exist until week 5 — six rows of ``precision 1/2``, and the whole reason
    compiled provenance sat below raw. The harness is bitemporal everywhere
    else; the plan was the one place that was not.

    Revisions chain through ``supersedes``, because a store that learns more
    about a claim revises the note it already holds rather than filing a second
    one. The first revision of a claim that superseded another points at that
    predecessor's *head*, so lineage crosses claim boundaries the way it does in
    the corpus.
    """

    disputes = _disputing_pairs(claims)
    # Conclusions must read like knowledge, not like a database dump. A note
    # titled "headcount of ENT-8E7A0887" is lexically unreachable — a query
    # naming the organisation would never match it — so a compiled altitude
    # built on raw ids would score zero for a reason that has nothing to do with
    # any contender. Entity ids are resolved to canonical names wherever known.
    names = {e.entity_id: e.canonical_name for e in (entities or [])}
    # Claim-id order is the stable baseline, but it cannot be the final plan
    # order: a successor must only be emitted after the conclusion it replaces.
    # Kahn's traversal preserves the baseline order whenever dependencies leave
    # a choice, while making that predecessor-before-successor contract explicit.
    baseline_claims = sorted(claims, key=lambda c: c.claim_id)
    claims_by_id = {claim.claim_id: claim for claim in baseline_claims}
    baseline_index = {
        claim.claim_id: index for index, claim in enumerate(baseline_claims)
    }
    dependents: dict[str, list[str]] = defaultdict(list)
    pending: dict[str, int] = {claim.claim_id: 0 for claim in baseline_claims}
    for claim in baseline_claims:
        if claim.supersedes in claims_by_id:
            dependents[claim.supersedes].append(claim.claim_id)
            pending[claim.claim_id] += 1

    ready = [claim.claim_id for claim in baseline_claims if not pending[claim.claim_id]]
    ordered_claims = []
    while ready:
        ready.sort(key=baseline_index.__getitem__)
        claim_id = ready.pop(0)
        ordered_claims.append(claims_by_id[claim_id])
        for successor_id in dependents[claim_id]:
            pending[successor_id] -= 1
            if not pending[successor_id]:
                ready.append(successor_id)
    if len(ordered_claims) != len(baseline_claims):
        cycle_claims = sorted(
            claim_id for claim_id, dependency_count in pending.items() if dependency_count
        )
        raise ValueError(
            "supersession cycle prevents compile-plan ordering: "
            + ", ".join(cycle_claims)
        )
    # The head of each claim's chain, so lineage and disputes can point at the
    # revision a store would actually be holding rather than at its first draft.
    heads = {
        claim.claim_id: conclusion_id_for(claim.claim_id, revisions[-1][0])
        for claim in ordered_claims
        if (revisions := _supporting_by_week(claim))
    }

    plan: list[ConclusionRecord] = []
    index = 0
    for claim in ordered_claims:
        revisions = _supporting_by_week(claim)
        if not revisions:
            # A conclusion with no basis is the 4b.13 defect: citation precision
            # is unverifiable there, which is the one place a shotgun passes for
            # free. Refuse at derivation rather than emit an unscoreable record.
            raise ValueError(
                f"claim {claim.claim_id} has no supporting source; a conclusion "
                "with no basis makes citation precision unverifiable"
            )
        previous: str | None = (
            heads.get(claim.supersedes) if claim.supersedes else None
        )
        for knowledge_week, cites in revisions:
            conclusion_id = conclusion_id_for(claim.claim_id, knowledge_week)
            plan.append(
                ConclusionRecord(
                    conclusion_id=conclusion_id,
                    claim_id=claim.claim_id,
                    knowledge_week=knowledge_week,
                    title=_phrase(claim, names),
                    body=(
                        f"{_phrase(claim, names)} is {claim.object.value}"
                        + (f" {claim.object.unit}" if claim.object.unit else "")
                        + "."
                    ),
                    cites=cites,
                    supersedes=previous,
                    disputes=tuple(
                        heads[other]
                        for other in disputes.get(claim.claim_id, ())
                        if other in heads
                    ),
                    sort_key=index,
                )
            )
            previous = conclusion_id
            index += 1
    return plan
