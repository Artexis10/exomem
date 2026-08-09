"""T08 — two firsthand accounts disagree and the conflict stays unresolved."""

from __future__ import annotations

from membench import wordbank
from membench.clock import week_date
from membench.schema import AuthorityTier, TypedValue
from membench.templates.base import BuildContext, Template, expect_value, register
from membench.templates.builders_ext import expect_disputed

TEMPLATE_ID = "t08_equal_authority_dispute"
FAMILY = "epistemics"


def build(ctx: BuildContext) -> None:
    project = ctx.entity("project", "business")
    pname = project.canonical_name
    person_a = ctx.person()
    person_b = ctx.person()
    while person_b == person_a:
        person_b = ctx.person()
    date_a = week_date(9, ctx.rng.randrange(0, 5)).isoformat()
    date_b = week_date(10, ctx.rng.randrange(0, 5)).isoformat()
    venue = f"the {wordbank.noun(ctx.rng)} hall"

    s_a = ctx.source(
        3,
        f"{pname} planning note by {person_a}",
        authority=AuthorityTier.FIRSTHAND,
        lines=[
            f"{person_a} recorded the {pname} demo date as {date_a}.",
            f"{person_a} booked {venue} for the {pname} demo.",
        ],
    )
    c_date_a = ctx.claim(
        project, "demo_date", TypedValue(kind="date", value=date_a), s_a
    )
    c_venue = ctx.claim(
        project, "demo_venue", TypedValue(kind="text", value=venue), s_a
    )

    s_b = ctx.source(
        5,
        f"{pname} planning note by {person_b}",
        authority=AuthorityTier.FIRSTHAND,
        lines=[f"{person_b} recorded the {pname} demo date as {date_b}."],
    )
    c_date_b = ctx.claim(
        project, "demo_date", TypedValue(kind="date", value=date_b), s_b
    )
    ctx.dispute(c_date_a, s_b, week=5)
    ctx.dispute(c_date_b, s_a, week=5)

    ctx.query(
        "current_truth",
        f"When is the {pname} demo scheduled?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_disputed(c_date_a, c_date_b),
    )
    ctx.query(
        "current_truth",
        f"When was the {pname} demo scheduled per the first planning note?",
        knowledge_week=4,
        family=FAMILY,
        expect=expect_value(c_date_a),
    )
    ctx.query(
        "direct_recall",
        f"Where is the {pname} demo being held?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_value(c_venue),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Equal-authority disagreement stays disputed: hedge and cite both sides",
        variants=4,
        build=build,
    )
)
