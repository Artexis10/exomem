"""T19 — recorded-false is not unknown: rejected proposals stay answerable.

Three deterministic ground-truth cases per variant:

1. RECORDED-FALSE: a proposal the corpus records as rejected (DISPROVED).
   The correct answer states the rejection with the rejecting source's
   citation; abstaining fails the abstention gate and asserting the proposal
   as active fails the current-state gate.
2. CONSIDERED-AND-REJECTED plan: a plan claim later revoked by an official
   decision; "did we go with X?" expects the rejection plus its citation.
3. NOT-RECORDED sibling: a plausible sibling proposal never recorded, asked
   with near-identical phrasing to case 1 — abstention required.

Plus one as-of query about the pre-rejection window and one pre-rejection
knowledge-cutoff query, both hedged tentative views.
"""

from __future__ import annotations

from membench import wordbank
from membench.schema import AuthorityTier, ClaimStatus, TypedValue
from membench.templates.base import (
    BuildContext,
    GenerationError,
    Template,
    expect_abstain,
    expect_value,
    register,
)
from membench.templates.builders_ext import expect_recorded_false

TEMPLATE_ID = "t19_negation_counterfactual"
FAMILY = "negation_counterfactual"


def _distinct_product(ctx: BuildContext, taken: str) -> str:
    """A sibling product name that was never recorded anywhere in the corpus."""

    for _ in range(32):
        candidate = wordbank.product_name(ctx.rng)
        if candidate != taken:
            return candidate
    raise GenerationError(f"{ctx.template_id}: no distinct sibling product name")


def build(ctx: BuildContext) -> None:
    project = ctx.entity("project", "technology")
    org = ctx.entity("organization", "business")
    pname = project.canonical_name
    oname = org.canonical_name
    candidate = wordbank.product_name(ctx.rng)
    sibling = _distinct_product(ctx, candidate)

    proposal_value = f"adopting the {candidate} toolchain for reporting"
    rejection_value = (
        f"rejected the {candidate} toolchain; reporting stays on the standing stack"
    )
    plan_value = f"running a joint pilot with {oname}"
    plan_decision_value = f"declined the {oname} pilot; no pilot is planned"

    # Case 1 — proposal recorded, then recorded as rejected (DISPROVED).
    s_proposal = ctx.source(
        2,
        f"{pname} reporting toolchain proposal",
        lines=[
            f"Proposal drafted: {pname} would be adopting the {candidate} "
            "toolchain for reporting.",
            "A decision is expected within the month.",
        ],
    )
    c_proposal = ctx.claim(
        project,
        "reporting_toolchain_proposal",
        TypedValue(kind="text", value=proposal_value),
        s_proposal,
        status=ClaimStatus.TENTATIVE,
    )
    s_decision = ctx.source(
        6,
        f"{pname} toolchain decision",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"Decision recorded: {pname} rejected the {candidate} toolchain; "
            "reporting stays on the standing stack.",
        ],
    )
    ctx.disprove(c_proposal, s_decision, week=6)
    c_rejection = ctx.claim(
        project,
        "reporting_toolchain_decision",
        TypedValue(kind="text", value=rejection_value),
        s_decision,
    )

    # Case 2 — a plan considered, then rejected (REVOKED by the decision).
    s_plan = ctx.source(
        3,
        f"{pname} pilot plan under consideration",
        lines=[
            f"Under consideration: {pname} running a joint pilot with {oname} "
            "next quarter.",
        ],
    )
    c_plan = ctx.claim(
        project,
        "pilot_plan",
        TypedValue(kind="text", value=plan_value),
        s_plan,
        status=ClaimStatus.TENTATIVE,
    )
    s_plan_decision = ctx.source(
        7,
        f"{pname} pilot decision",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"Outcome: {pname} declined the {oname} pilot; no pilot is planned.",
        ],
    )
    ctx.revoke(c_plan, s_plan_decision, week=7)
    c_plan_decision = ctx.claim(
        project,
        "pilot_decision",
        TypedValue(kind="text", value=plan_decision_value),
        s_plan_decision,
    )

    # Case 1: recorded-false is answered with the rejection, never abstained.
    ctx.query(
        "current_truth",
        f"Did {pname} adopt the {candidate} toolchain for reporting?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_recorded_false(c_rejection, c_proposal, s_decision),
    )
    # Case 2: considered-and-rejected plan.
    ctx.query(
        "current_truth",
        f"Did {pname} go ahead with the {oname} pilot?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_recorded_false(c_plan_decision, c_plan, s_plan_decision),
    )
    # Case 3: not-recorded sibling, near-identical phrasing to case 1.
    ctx.query(
        "unanswerable",
        f"Did {pname} adopt the {sibling} toolchain for reporting?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_abstain(),
    )
    # Pre-rejection window, world-time: the proposal was live (tentatively).
    ctx.query(
        "as_of",
        f"As of week 4, what was the standing of the {candidate} toolchain "
        f"proposal for {pname}?",
        knowledge_week=9,
        world_week=4,
        family=FAMILY,
        expect=expect_value(c_proposal, hedged=True),
    )
    # Pre-rejection window, knowledge-time: nothing rejected is visible yet.
    ctx.query(
        "current_truth",
        f"What is the standing of the {candidate} toolchain proposal for {pname}?",
        knowledge_week=4,
        family=FAMILY,
        expect=expect_value(c_proposal, hedged=True),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Recorded-false answered with rejection; not-recorded sibling abstained",
        variants=4,
        build=build,
    )
)
