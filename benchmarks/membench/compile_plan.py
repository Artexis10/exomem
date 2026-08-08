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

from pydantic import Field

from membench.ids import stable_id
from membench.schema import ClaimRecord, Stance, StrictModel


class ConclusionRecord(StrictModel):
    """A durable conclusion, as a knowledge store would hold it.

    Field names are deliberately product-neutral. ``cites`` rather than
    ``sources``, because the latter is exomem's ``remember`` parameter and would
    bias the whole contract toward one contender's grammar.
    """

    conclusion_id: str
    #: The oracle claim this conclusion states. Keeps the plan joinable back to
    #: expectations without duplicating the claim's contents.
    claim_id: str
    title: str
    body: str
    #: Source ids this conclusion draws from, in recorded order.
    cites: tuple[str, ...] = ()
    #: The conclusion this one replaces, when the underlying claim superseded
    #: another. Lineage, not a dispute.
    supersedes: str | None = None
    #: Conclusions asserting an incompatible value for the same subject and
    #: predicate, live at the same time. Symmetric.
    disputes: tuple[str, ...] = ()
    #: Ordering key, so a plan is stable regardless of input order.
    sort_key: int = Field(default=0, ge=0)


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


def conclusion_id_for(claim_id: str) -> str:
    """Stable, content-free id. One conclusion per claim, by construction."""

    return stable_id("CON", claim_id)


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


def derive_compile_plan(claims: list[ClaimRecord]) -> list[ConclusionRecord]:
    """One conclusion per claim, ordered by claim id so the plan is stable.

    Stability is load-bearing rather than tidy: the plan is written into the
    corpus and hashed into the release manifest, so an ordering that depended on
    input order would make generated bytes depend on template execution order.
    """

    disputes = _disputing_pairs(claims)
    plan: list[ConclusionRecord] = []
    for index, claim in enumerate(sorted(claims, key=lambda c: c.claim_id)):
        cites = _supporting_sources(claim)
        if not cites:
            # A conclusion with no basis is the 4b.13 defect: citation precision
            # is unverifiable there, which is the one place a shotgun passes for
            # free. Refuse at derivation rather than emit an unscoreable record.
            raise ValueError(
                f"claim {claim.claim_id} has no supporting source; a conclusion "
                "with no basis makes citation precision unverifiable"
            )
        plan.append(
            ConclusionRecord(
                conclusion_id=conclusion_id_for(claim.claim_id),
                claim_id=claim.claim_id,
                title=f"{claim.predicate} of {claim.subject}",
                body=(
                    f"{claim.predicate} of {claim.subject} is "
                    f"{claim.object.value}"
                    + (f" {claim.object.unit}" if claim.object.unit else "")
                    + "."
                ),
                cites=cites,
                supersedes=(
                    conclusion_id_for(claim.supersedes) if claim.supersedes else None
                ),
                disputes=tuple(
                    conclusion_id_for(other) for other in disputes.get(claim.claim_id, ())
                ),
                sort_key=index,
            )
        )
    return plan
