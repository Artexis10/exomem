"""T10 — an official statement is formally retracted and must not be repeated."""

from __future__ import annotations

from membench import wordbank
from membench.schema import AuthorityTier, TypedValue
from membench.templates.base import BuildContext, Template, expect_value, register
from membench.templates.builders_ext import expect_revoked

TEMPLATE_ID = "t10_retraction"
FAMILY = "epistemics"


def build(ctx: BuildContext) -> None:
    org = ctx.entity("organization", "business")
    oname = org.canonical_name
    market = wordbank.city_name(ctx.rng)
    statement = f"expansion into the {market} market"

    s_announce = ctx.source(
        2,
        f"{oname} expansion announcement",
        authority=AuthorityTier.OFFICIAL,
        lines=[f"{oname} announced expansion into the {market} market."],
    )
    c_statement = ctx.claim(
        org,
        "announced_initiative",
        TypedValue(kind="text", value=statement),
        s_announce,
    )

    s_retract = ctx.source(
        6,
        f"{oname} formal retraction",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"{oname} formally withdrew its earlier announcement.",
            f"The {market} expansion is off; treat the prior statement as retracted.",
        ],
    )
    ctx.revoke(c_statement, s_retract, week=6)

    ctx.query(
        "current_truth",
        f"What initiative does {oname} currently have announced?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_revoked(c_statement),
    )
    ctx.query(
        "current_truth",
        f"What initiative had {oname} announced?",
        knowledge_week=4,
        family=FAMILY,
        expect=expect_value(c_statement),
    )
    ctx.query(
        "as_of",
        f"As of week 3, what initiative had {oname} announced?",
        knowledge_week=9,
        world_week=3,
        family=FAMILY,
        expect=expect_value(c_statement),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Formal retraction: revoked statement abstained on, history still as-of",
        variants=4,
        build=build,
    )
)
