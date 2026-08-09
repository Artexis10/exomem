"""T05 — a planned future milestone must not be reported as completed fact."""

from __future__ import annotations

from membench.clock import week_date
from membench.schema import AuthorityTier, ClaimStatus, TypedValue
from membench.templates.base import (
    BuildContext,
    Template,
    expect_abstain,
    expect_value,
    register,
)

TEMPLATE_ID = "t05_future_plans"
FAMILY = "temporal"


def build(ctx: BuildContext) -> None:
    project = ctx.entity("project", "technology")
    pname = project.canonical_name
    planned = week_date(10, ctx.rng.randrange(0, 5)).isoformat()

    s_plan = ctx.source(
        2,
        f"{pname} rollout plan",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"The team penciled in the {pname} launch for {planned}.",
            f"The {pname} launch date remains a target, not a commitment.",
        ],
    )
    c_launch = ctx.claim(
        project,
        "planned_launch_date",
        TypedValue(kind="date", value=planned),
        s_plan,
        status=ClaimStatus.TENTATIVE,
    )

    ctx.query(
        "current_truth",
        f"Has {pname} launched yet, and when is the launch expected?",
        knowledge_week=6,
        family=FAMILY,
        expect=expect_value(c_launch, hedged=True),
    )
    ctx.query(
        "unanswerable",
        f"On what date did {pname} actually complete its launch?",
        knowledge_week=6,
        family=FAMILY,
        expect=expect_abstain(),
    )
    ctx.query(
        "direct_recall",
        f"What launch date is currently penciled in for {pname}?",
        knowledge_week=6,
        family=FAMILY,
        expect=expect_value(c_launch, hedged=True),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Tentative future milestone: hedged plan answers, abstain on completion",
        variants=4,
        build=build,
    )
)
