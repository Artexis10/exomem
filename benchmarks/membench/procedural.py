"""Ordered-steps (procedural) claim shape composed from existing primitives.

A how-to procedure is modelled as ordinary corpus records — no schema or
oracle change:

- each step is a claim (predicate ``<slug>-step-<n>`` whose ``TypedValue``
  names the step),
- the step-before-X relation is a claim whose ``TypedValue`` names the
  predecessor step,
- preconditions are claims,
- a procedure revision is a supersession of the affected step-order claims
  by a revising source.

The module is pure composition: no import side effects, no I/O, no clock.
All randomness comes from the caller's :class:`BuildContext` rng via the
shared wordbank, and every expectation derives exclusively through existing
oracle calls (``truth_at``/``current_truth``/``required_citations``) — the
builders self-check the authored shape and refuse to generate an
inconsistent corpus.
"""

from __future__ import annotations

from dataclasses import dataclass
from random import Random

from membench import oracle, wordbank
from membench.ids import slugify
from membench.schema import (
    AuthorityTier,
    ClaimRecord,
    ClaimStatus,
    EntityRecord,
    ExpectedAnswer,
    ExpectedRecord,
    QueryRecord,
    SourceRecord,
    TypedValue,
)
from membench.templates.base import (
    BuildContext,
    ExpectationBuilder,
    GenerationError,
    OracleCtx,
    _answer_from_value,
    _view_for,
)

_RETIRED_STATUSES = (ClaimStatus.SUPERSEDED, ClaimStatus.PARTIALLY_SUPERSEDED)


def distinct_nouns(rng: Random, count: int) -> list[str]:
    """``count`` distinct wordbank nouns, drawn deterministically from ``rng``."""

    seen: dict[str, None] = {}
    attempts = 0
    while len(seen) < count:
        attempts += 1
        if attempts > 1000:
            raise GenerationError(
                f"could not draw {count} distinct nouns from the wordbank"
            )
        seen.setdefault(wordbank.noun(rng))
    return list(seen)


def step_predicate(slug: str, position: int) -> str:
    return f"{slug}-step-{position}"


def predecessor_predicate(slug: str, target_label: str) -> str:
    return f"{slug}-before-{slugify(target_label)}"


def precondition_predicate(slug: str) -> str:
    return f"{slug}-precondition"


def _guard_labels(labels: list[str], *, extra: str | None = None) -> None:
    """Step labels must be pairwise distinct and containment-free, or the
    substring checks of the value/current-state gates would collide."""

    pool = list(labels) + ([extra] if extra is not None else [])
    for label in pool:
        if not label.strip():
            raise GenerationError("empty step label")
    for index, first in enumerate(pool):
        for second in pool[index + 1 :]:
            if first == second or first in second or second in first:
                raise GenerationError(
                    f"step labels overlap: {first!r} vs {second!r}"
                )


@dataclass
class ProcedureSteps:
    """One authored procedure: entity, ordered labels, and its claims."""

    procedure: EntityRecord
    slug: str
    labels: tuple[str, ...]  # authored order, position 1..n
    manual: SourceRecord
    position_claims: dict[int, ClaimRecord]  # position -> step claim
    predecessor_claims: dict[int, ClaimRecord]  # target position -> claim
    precondition_claim: ClaimRecord | None


@dataclass
class ProcedureRevision:
    """One revision: the replacing step and every superseded order claim."""

    source: SourceRecord
    position: int
    replacement_label: str
    old_position_claim: ClaimRecord
    new_position_claim: ClaimRecord
    old_predecessor_claims: dict[int, ClaimRecord]  # target position -> claim
    new_predecessor_claims: dict[int, ClaimRecord]


def author_procedure(
    ctx: BuildContext,
    *,
    week: int,
    name: str,
    labels: list[str],
    predecessor_targets: list[int] | None = None,
    precondition_label: str | None = None,
    authority: AuthorityTier = AuthorityTier.OFFICIAL,
) -> ProcedureSteps:
    """Author the ordered-steps shape: one manual source, one claim per step,
    an explicit step-before claim per requested target, and an optional
    precondition claim. Every claim value appears verbatim in the manual."""

    if len(labels) < 2:
        raise GenerationError("a procedure needs at least two steps")
    _guard_labels(labels)
    targets = list(predecessor_targets or [])
    for target in targets:
        if not 2 <= target <= len(labels):
            raise GenerationError(f"predecessor target {target} out of range")

    procedure = ctx.entity("concept", "operations", name=name)
    slug = slugify(name)
    lines = [f"This manual records the {name} as a strictly ordered procedure."]
    if precondition_label is not None:
        lines.append(
            f"Before step 1 can start, the {precondition_label} must be in place."
        )
    for position, label in enumerate(labels, start=1):
        lines.append(f"Step {position} of the {name} is the {label}.")
    for target in targets:
        lines.append(
            f"The step directly before the {labels[target - 1]} is the "
            f"{labels[target - 2]}."
        )
    manual = ctx.source(week, f"{name} operating manual", authority=authority, lines=lines)

    position_claims = {
        position: ctx.claim(
            procedure,
            step_predicate(slug, position),
            TypedValue(kind="text", value=label),
            manual,
        )
        for position, label in enumerate(labels, start=1)
    }
    predecessor_claims = {
        target: ctx.claim(
            procedure,
            predecessor_predicate(slug, labels[target - 1]),
            TypedValue(kind="text", value=labels[target - 2]),
            manual,
        )
        for target in targets
    }
    precondition_claim = None
    if precondition_label is not None:
        precondition_claim = ctx.claim(
            procedure,
            precondition_predicate(slug),
            TypedValue(kind="text", value=precondition_label),
            manual,
        )
    return ProcedureSteps(
        procedure=procedure,
        slug=slug,
        labels=tuple(labels),
        manual=manual,
        position_claims=position_claims,
        predecessor_claims=predecessor_claims,
        precondition_claim=precondition_claim,
    )


def revise_step(
    ctx: BuildContext,
    proc: ProcedureSteps,
    *,
    week: int,
    position: int,
    replacement_label: str,
    authority: AuthorityTier = AuthorityTier.OFFICIAL,
) -> ProcedureRevision:
    """Revise the procedure: the step at ``position`` is replaced by
    ``replacement_label``. The revising source supersedes the affected
    step-order claims (the position claim and every step-before claim whose
    predecessor was the retired step)."""

    if position not in proc.position_claims:
        raise GenerationError(f"no step at position {position} to revise")
    _guard_labels(list(proc.labels), extra=replacement_label)

    name = proc.procedure.canonical_name
    old_label = proc.labels[position - 1]
    affected = [t for t in sorted(proc.predecessor_claims) if t - 1 == position]

    lines = [
        f"Revision memo for the {name}.",
        f"The {old_label} is retired from the {name} effective immediately.",
        f"Step {position} of the {name} is now the {replacement_label}.",
    ]
    for target in affected:
        lines.append(
            f"The step directly before the {proc.labels[target - 1]} is now "
            f"the {replacement_label}."
        )
    source = ctx.source(week, f"{name} revision memo", authority=authority, lines=lines)

    old_position_claim = proc.position_claims[position]
    new_position_claim = ctx.claim(
        proc.procedure,
        step_predicate(proc.slug, position),
        TypedValue(kind="text", value=replacement_label),
        source,
    )
    ctx.supersede(old_position_claim, new_position_claim, week=week)

    old_predecessors: dict[int, ClaimRecord] = {}
    new_predecessors: dict[int, ClaimRecord] = {}
    for target in affected:
        old_claim = proc.predecessor_claims[target]
        new_claim = ctx.claim(
            proc.procedure,
            predecessor_predicate(proc.slug, proc.labels[target - 1]),
            TypedValue(kind="text", value=replacement_label),
            source,
        )
        ctx.supersede(old_claim, new_claim, week=week)
        old_predecessors[target] = old_claim
        new_predecessors[target] = new_claim

    return ProcedureRevision(
        source=source,
        position=position,
        replacement_label=replacement_label,
        old_position_claim=old_position_claim,
        new_position_claim=new_position_claim,
        old_predecessor_claims=old_predecessors,
        new_predecessor_claims=new_predecessors,
    )


def current_order(
    proc: ProcedureSteps, revision: ProcedureRevision | None = None
) -> tuple[list[str], list[ClaimRecord], list[ClaimRecord]]:
    """(labels in current order, current position claims in order, retired
    position claims) after applying ``revision`` to the authored procedure."""

    labels = list(proc.labels)
    claims = dict(proc.position_claims)
    retired: list[ClaimRecord] = []
    if revision is not None:
        labels[revision.position - 1] = revision.replacement_label
        retired.append(revision.old_position_claim)
        claims[revision.position] = revision.new_position_claim
    ordered = [claims[position] for position in sorted(claims)]
    return labels, ordered, retired


# -- expectation builders (oracle-derived, evaluated at finalize) ---------


def _citations(
    ctx: OracleCtx, claim: ClaimRecord, view: oracle.TruthView, query: QueryRecord
) -> tuple[str, ...]:
    return oracle.required_citations(
        claim, view, claims_by_id=ctx.claims_by_id, knowledge_week=query.ask.knowledge_week
    )


def expect_post_revision_predecessor(
    new: ClaimRecord, old: ClaimRecord, revising_source: SourceRecord
) -> ExpectationBuilder:
    """Binding spec scenario: the expected record names the post-revision
    predecessor step and the revising source's citation; the pre-revision
    predecessor is a forbidden claim, so an answer giving it fails the
    current-state gate."""

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        new_view = _view_for(ctx, new, query)
        old_view = _view_for(ctx, old, query)
        if not new_view.is_active or new_view.value is None:
            raise GenerationError(
                f"{query.query_id}: post-revision predecessor {new.claim_id} is "
                f"inactive ({new_view.status.value})"
            )
        if old_view.status not in _RETIRED_STATUSES:
            raise GenerationError(
                f"{query.query_id}: pre-revision predecessor {old.claim_id} should "
                f"be superseded, got {old_view.status.value}"
            )
        if old.superseded_by != new.claim_id:
            raise GenerationError(
                f"{query.query_id}: {old.claim_id} is not superseded by {new.claim_id}"
            )
        old_value = old.object.value
        new_value = new_view.value.value
        if old_value == new_value or old_value in new_value or new_value in old_value:
            raise GenerationError(
                f"{query.query_id}: pre- and post-revision predecessors overlap "
                f"({old_value!r} vs {new_value!r})"
            )
        citations = _citations(ctx, new, new_view, query)
        if revising_source.source_id not in citations:
            raise GenerationError(
                f"{query.query_id}: revising source {revising_source.source_id} "
                "missing from oracle citations"
            )
        return ExpectedRecord(
            query_id=query.query_id,
            answer=_answer_from_value(new_view.value),
            required_claims=[new.claim_id],
            forbidden_claims=[old.claim_id],
            required_citations=list(citations),
            gates=["current_state", "citations"],
        )

    return build


def expect_current_order(
    ordered: list[ClaimRecord], *, retired: list[ClaimRecord]
) -> ExpectationBuilder:
    """Current-order recall: every current step claim required, in order, as
    a list answer; retired step claims are forbidden."""

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        values: list[str] = []
        citations: dict[str, None] = {}
        for claim in ordered:
            view = _view_for(ctx, claim, query)
            if not view.is_active or view.value is None:
                raise GenerationError(
                    f"{query.query_id}: step claim {claim.claim_id} is inactive "
                    f"({view.status.value})"
                )
            values.append(view.value.value)
            for source_id in _citations(ctx, claim, view, query):
                citations.setdefault(source_id)
        if len(set(values)) != len(values):
            raise GenerationError(f"{query.query_id}: duplicate step labels {values}")
        for claim in retired:
            view = _view_for(ctx, claim, query)
            if view.status not in _RETIRED_STATUSES:
                raise GenerationError(
                    f"{query.query_id}: retired step {claim.claim_id} should be "
                    f"superseded, got {view.status.value}"
                )
            retired_value = claim.object.value
            if any(retired_value in v or v in retired_value for v in values):
                raise GenerationError(
                    f"{query.query_id}: retired label {retired_value!r} overlaps "
                    "a current step label"
                )
        return ExpectedRecord(
            query_id=query.query_id,
            answer=ExpectedAnswer(kind="list", values=values),
            required_claims=[c.claim_id for c in ordered],
            forbidden_claims=[c.claim_id for c in retired],
            required_citations=list(citations),
            gates=["current_state", "citations"],
        )

    return build
