"""T02 — two-clause decision where only one clause changes (partial supersession)."""

from __future__ import annotations

from membench.schema import AuthorityTier, TypedValue
from membench.templates.base import (
    BuildContext,
    Template,
    expect_superseded_history,
    expect_value,
    register,
)

TEMPLATE_ID = "t02_partial_supersession"
FAMILY = "temporal"


def build(ctx: BuildContext) -> None:
    project = ctx.entity("project", "business")
    pname = project.canonical_name
    budget_v1 = ctx.rng.randrange(40, 90) * 1000
    budget_v2 = budget_v1 + ctx.rng.randrange(2, 9) * 1000

    s_decision = ctx.source(
        1,
        f"{pname} steering decision",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"Decision for {pname}, clause one: reviews happen every Tuesday.",
            f"Decision for {pname}, clause two: the pilot budget is {budget_v1} credits.",
        ],
    )
    c_cadence = ctx.claim(
        project,
        "review_cadence",
        TypedValue(kind="text", value="every Tuesday"),
        s_decision,
    )
    c_budget_v1 = ctx.claim(
        project,
        "pilot_budget",
        TypedValue(kind="quantity", value=str(budget_v1), unit="credits"),
        s_decision,
    )

    s_amend = ctx.source(
        5,
        f"{pname} budget amendment",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"Only the budget clause of the {pname} steering decision changes.",
            f"The pilot budget for {pname} is now {budget_v2} credits.",
            f"Reviews for {pname} stay on every Tuesday, unchanged.",
        ],
    )
    c_budget_v2 = ctx.claim(
        project,
        "pilot_budget",
        TypedValue(kind="quantity", value=str(budget_v2), unit="credits"),
        s_amend,
    )
    ctx.supersede(c_budget_v1, c_budget_v2, week=5, partial=True)

    ctx.query(
        "current_truth",
        f"What is the current review cadence for {pname}?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_value(c_cadence),
    )
    ctx.query(
        "current_truth",
        f"What is the current pilot budget for {pname}?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_value(c_budget_v2, forbidden=[c_budget_v1]),
    )
    ctx.query(
        "as_of",
        f"What was the pilot budget for {pname} as of week 3, before the amendment?",
        knowledge_week=8,
        world_week=3,
        family=FAMILY,
        expect=expect_superseded_history(c_budget_v1, c_budget_v2),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="One clause of a two-clause decision changes; the other stays current",
        variants=4,
        build=build,
    )
)
