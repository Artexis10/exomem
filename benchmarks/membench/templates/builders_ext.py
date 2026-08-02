"""Shared oracle-derived expectation builders for the family templates.

Every helper returns an :data:`ExpectationBuilder` closure that is evaluated
at finalize time against the finished scenario graph. Builders self-check:
when the oracle view contradicts what the template meant to author, they
raise :class:`GenerationError` so an inconsistent corpus refuses to generate.
"""

from __future__ import annotations

from membench import oracle, quant
from membench.schema import (
    ClaimRecord,
    ClaimStatus,
    ExpectedAnswer,
    ExpectedRecord,
    QueryRecord,
    SourceRecord,
    UncertaintyExpectation,
)
from membench.templates.base import (
    ExpectationBuilder,
    GenerationError,
    OracleCtx,
    _answer_from_value,
    _view_for,
    expect_value,
)


def _citations_for(
    ctx: OracleCtx, claim: ClaimRecord, view: oracle.TruthView, query: QueryRecord
) -> tuple[str, ...]:
    return oracle.required_citations(
        claim, view, claims_by_id=ctx.claims_by_id, knowledge_week=query.ask.knowledge_week
    )


def expect_change(
    old: ClaimRecord, new: ClaimRecord, reversal_source: SourceRecord
) -> ExpectationBuilder:
    """What-changed-and-why: both claim ids required, reversal source cited."""

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        old_view = _view_for(ctx, old, query)
        new_view = _view_for(ctx, new, query)
        if old_view.status not in (
            ClaimStatus.SUPERSEDED,
            ClaimStatus.PARTIALLY_SUPERSEDED,
        ):
            raise GenerationError(
                f"{query.query_id}: change query needs {old.claim_id} superseded, "
                f"got {old_view.status.value}"
            )
        if old.superseded_by != new.claim_id:
            raise GenerationError(
                f"{query.query_id}: {old.claim_id} is not superseded by {new.claim_id}"
            )
        if not new_view.is_active:
            raise GenerationError(
                f"{query.query_id}: replacement {new.claim_id} is inactive "
                f"({new_view.status.value})"
            )
        ordered: dict[str, None] = {}
        for source_id in _citations_for(ctx, new, new_view, query):
            ordered.setdefault(source_id)
        for source_id in _citations_for(ctx, old, old_view, query):
            ordered.setdefault(source_id)
        if reversal_source.source_id not in ordered:
            raise GenerationError(
                f"{query.query_id}: reversal source {reversal_source.source_id} "
                "missing from oracle citations"
            )
        return ExpectedRecord(
            query_id=query.query_id,
            answer=ExpectedAnswer(
                kind="text",
                values=[old.object.value, new.object.value],
                unit=new.object.unit,
            ),
            required_claims=[old.claim_id, new.claim_id],
            required_citations=list(ordered),
            gates=["change_explanation", "citations"],
        )

    return build


def expect_unknown_abstain(claim: ClaimRecord) -> ExpectationBuilder:
    """Oracle must see UNKNOWN at the asked time; the answer is an abstention."""

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        view = _view_for(ctx, claim, query)
        if view.status is not ClaimStatus.UNKNOWN:
            raise GenerationError(
                f"{query.query_id}: expected UNKNOWN for {claim.claim_id}, "
                f"got {view.status.value}"
            )
        return ExpectedRecord(
            query_id=query.query_id,
            answer=ExpectedAnswer(kind="none"),
            abstain=True,
            forbidden_claims=[claim.claim_id],
            gates=["abstention"],
        )

    return build


def expect_value_with_dispute(
    authoritative: ClaimRecord, conflicting: ClaimRecord
) -> ExpectationBuilder:
    """Authoritative value wins, but the answer must acknowledge the conflict."""

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        view = _view_for(ctx, authoritative, query)
        if not view.is_active:
            raise GenerationError(
                f"{query.query_id}: authoritative claim {authoritative.claim_id} is "
                f"inactive ({view.status.value})"
            )
        conflict_view = _view_for(ctx, conflicting, query)
        if conflict_view.status not in (ClaimStatus.TENTATIVE, ClaimStatus.DISPUTED):
            raise GenerationError(
                f"{query.query_id}: conflicting claim {conflicting.claim_id} should be "
                f"tentative or disputed, got {conflict_view.status.value}"
            )
        if conflicting.object == authoritative.object:
            raise GenerationError(
                f"{query.query_id}: {conflicting.claim_id} does not actually conflict "
                f"with {authoritative.claim_id}"
            )
        citations = _citations_for(ctx, authoritative, view, query)
        return ExpectedRecord(
            query_id=query.query_id,
            answer=_answer_from_value(view.value),
            required_claims=[authoritative.claim_id],
            required_citations=list(citations),
            uncertainty=UncertaintyExpectation(hedged=None, mention_dispute=True),
            gates=["current_state", "citations", "conflict_awareness"],
        )

    return build


def expect_disputed(first: ClaimRecord, second: ClaimRecord) -> ExpectationBuilder:
    """Unresolved equal-authority conflict: hedge and cite both sides."""

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        ordered: dict[str, None] = {}
        for claim in (first, second):
            view = _view_for(ctx, claim, query)
            if view.status is not ClaimStatus.DISPUTED:
                raise GenerationError(
                    f"{query.query_id}: expected DISPUTED for {claim.claim_id}, "
                    f"got {view.status.value}"
                )
            for source_id in _citations_for(ctx, claim, view, query):
                ordered.setdefault(source_id)
        if len(ordered) < 2:
            raise GenerationError(
                f"{query.query_id}: disputed pair yields fewer than two citations"
            )
        return ExpectedRecord(
            query_id=query.query_id,
            answer=ExpectedAnswer(
                kind="text",
                values=[first.object.value, second.object.value],
                unit=first.object.unit,
            ),
            required_claims=[first.claim_id, second.claim_id],
            required_citations=list(ordered),
            uncertainty=UncertaintyExpectation(
                hedged=True, mention_dispute=True, cite_both_sides=True
            ),
            gates=["conflict_awareness", "citations"],
        )

    return build


def expect_disproved(claim: ClaimRecord) -> ExpectationBuilder:
    """After disproof the claim must not be asserted; abstain instead."""

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        view = _view_for(ctx, claim, query)
        if view.status is not ClaimStatus.DISPROVED:
            raise GenerationError(
                f"{query.query_id}: expected DISPROVED for {claim.claim_id}, "
                f"got {view.status.value}"
            )
        return ExpectedRecord(
            query_id=query.query_id,
            answer=ExpectedAnswer(kind="none"),
            abstain=True,
            forbidden_claims=[claim.claim_id],
            gates=["abstention", "disproof_awareness"],
        )

    return build


def expect_revoked(claim: ClaimRecord) -> ExpectationBuilder:
    """A formally retracted statement must not be repeated as truth."""

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        view = _view_for(ctx, claim, query)
        if view.status is not ClaimStatus.REVOKED:
            raise GenerationError(
                f"{query.query_id}: expected REVOKED for {claim.claim_id}, "
                f"got {view.status.value}"
            )
        return ExpectedRecord(
            query_id=query.query_id,
            answer=ExpectedAnswer(kind="none"),
            abstain=True,
            forbidden_claims=[claim.claim_id],
            gates=["abstention", "retraction_awareness"],
        )

    return build


def expect_converted_value(
    claim: ClaimRecord, converted_value: str, converted_unit: str
) -> ExpectationBuilder:
    """Unit-conversion answer: the converted and the stated value both accepted."""

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        view = _view_for(ctx, claim, query)
        if not view.is_active or view.value is None:
            raise GenerationError(
                f"{query.query_id}: conversion needs an active value for "
                f"{claim.claim_id}, got {view.status.value}"
            )
        original = view.value
        try:
            float(converted_value)
            float(original.value)
        except ValueError as exc:
            raise GenerationError(
                f"{query.query_id}: conversion values must be numeric "
                f"({converted_value!r} / {original.value!r})"
            ) from exc
        if converted_value == original.value and converted_unit == original.unit:
            raise GenerationError(f"{query.query_id}: conversion changes nothing")
        citations = _citations_for(ctx, claim, view, query)
        return ExpectedRecord(
            query_id=query.query_id,
            answer=ExpectedAnswer(
                kind="value",
                values=[converted_value, original.value],
                unit=converted_unit,
            ),
            required_claims=[claim.claim_id],
            required_citations=list(citations),
            gates=["current_state", "citations", "unit_conversion"],
        )

    return build


def expect_derived_quantity(
    a: ClaimRecord,
    b: ClaimRecord,
    op: str,
    *,
    unit: str | None = None,
    places: int | None = None,
) -> ExpectationBuilder:
    """Quantitative-family answer: the oracle computes ``op`` over the two
    stored quantities via :mod:`membench.quant` and requires BOTH contributing
    sources as citations (the transitive ``derived_from`` rule stays intact
    because citations come from :func:`oracle.required_citations`)."""

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        views = []
        for claim in (a, b):
            view = _view_for(ctx, claim, query)
            if not view.is_active or view.value is None:
                raise GenerationError(
                    f"{query.query_id}: derived quantity needs an active value "
                    f"for {claim.claim_id}, got {view.status.value}"
                )
            views.append(view)
        try:
            derived = quant.derive(
                op, views[0].value, views[1].value, unit=unit, places=places
            )
        except quant.QuantityError as exc:
            raise GenerationError(f"{query.query_id}: {exc}") from exc
        ordered: dict[str, None] = {}
        for claim, view in zip((a, b), views):
            for source_id in _citations_for(ctx, claim, view, query):
                ordered.setdefault(source_id)
        if len(ordered) < 2:
            raise GenerationError(
                f"{query.query_id}: derived quantity needs two contributing "
                f"sources, got {sorted(ordered)}"
            )
        gates = ["derived_value", "citations"]
        operand_units = {views[0].value.unit, views[1].value.unit}
        if len(operand_units) > 1 or (
            derived.unit is not None and derived.unit not in operand_units
        ):
            gates.append("unit_conversion")
        return ExpectedRecord(
            query_id=query.query_id,
            answer=ExpectedAnswer(
                kind="value",
                values=[derived.value],
                unit=derived.unit,
                tolerance=float(derived.tolerance),
            ),
            required_citations=list(ordered),
            gates=gates,
        )

    return build


def expect_declassified(claim: ClaimRecord) -> ExpectationBuilder:
    """Value expectation that first proves the persona may now see the claim."""

    inner = expect_value(claim)

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        vis = oracle.visibility(
            ctx.graph.policy,
            query.persona,
            claim_id=claim.claim_id,
            at=oracle.world_cutoff(query.ask.knowledge_week),
        )
        if not vis.allowed:
            raise GenerationError(
                f"{query.query_id}: persona {query.persona} still restricted from "
                f"{claim.claim_id} at knowledge week {query.ask.knowledge_week}"
            )
        record = inner(ctx, query)
        return record.model_copy(update={"gates": [*record.gates, "declassification"]})

    return build


def expect_tombstoned(
    claim: ClaimRecord, source: SourceRecord, *, forbidden_values: list[str]
) -> ExpectationBuilder:
    """A tombstoned source's content must neither be cited nor disclosed."""

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        vis = oracle.visibility(
            ctx.graph.policy,
            query.persona,
            source_id=source.source_id,
            at=oracle.world_cutoff(query.ask.knowledge_week),
        )
        if not vis.tombstoned:
            raise GenerationError(
                f"{query.query_id}: source {source.source_id} is not tombstoned at "
                f"knowledge week {query.ask.knowledge_week}"
            )
        return ExpectedRecord(
            query_id=query.query_id,
            answer=ExpectedAnswer(kind="none"),
            abstain=True,
            forbidden_claims=[claim.claim_id],
            forbidden_disclosures=list(forbidden_values),
            gates=["no_leak", "abstention"],
        )

    return build


def expect_value_guarded(
    claim: ClaimRecord,
    *,
    forbidden_values: list[str],
    forbidden: list[ClaimRecord] | None = None,
) -> ExpectationBuilder:
    """Normal value expectation plus a no-leak guard for planted secrets."""

    inner = expect_value(claim, forbidden=forbidden)

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        record = inner(ctx, query)
        for value in forbidden_values:
            if any(value in answered for answered in record.answer.values):
                raise GenerationError(
                    f"{query.query_id}: guarded value {value!r} overlaps the "
                    "expected answer"
                )
        return record.model_copy(
            update={
                "forbidden_disclosures": list(forbidden_values),
                "gates": [*record.gates, "no_leak"],
            }
        )

    return build


def expect_recorded_false(
    rejection: ClaimRecord, rejected: ClaimRecord, rejecting_source: SourceRecord
) -> ExpectationBuilder:
    """Recorded-false is not unknown: state the rejection with its citation.

    The rejected proposal/plan must hold a DISPROVED or REVOKED span while the
    rejection-decision claim stays active. The expected answer asserts the
    rejection (so an abstention fails the abstention gate) and forbids the
    rejected claim's active framing (so an answer treating the proposal as
    live fails the current-state gate).
    """

    def build(ctx: OracleCtx, query: QueryRecord) -> ExpectedRecord:
        rejection_view = _view_for(ctx, rejection, query)
        if not rejection_view.is_active or rejection_view.value is None:
            raise GenerationError(
                f"{query.query_id}: rejection claim {rejection.claim_id} must be "
                f"active with a value, got {rejection_view.status.value}"
            )
        rejected_view = _view_for(ctx, rejected, query)
        if rejected_view.status not in (ClaimStatus.DISPROVED, ClaimStatus.REVOKED):
            raise GenerationError(
                f"{query.query_id}: rejected claim {rejected.claim_id} must be "
                f"DISPROVED or REVOKED, got {rejected_view.status.value}"
            )
        if rejected.object.value in rejection_view.value.value:
            raise GenerationError(
                f"{query.query_id}: rejection phrasing embeds the rejected value "
                f"{rejected.object.value!r}, so the current-state gate could "
                "never pass"
            )
        citations = _citations_for(ctx, rejection, rejection_view, query)
        if rejecting_source.source_id not in citations:
            raise GenerationError(
                f"{query.query_id}: rejecting source {rejecting_source.source_id} "
                "missing from oracle citations"
            )
        return ExpectedRecord(
            query_id=query.query_id,
            answer=_answer_from_value(rejection_view.value),
            required_claims=[rejection.claim_id],
            forbidden_claims=[rejected.claim_id],
            required_citations=list(citations),
            abstain=False,
            gates=["current_state", "citations", "abstention"],
        )

    return build
