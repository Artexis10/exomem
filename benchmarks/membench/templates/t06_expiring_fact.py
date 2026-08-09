"""T06 — an offer valid until week 8: answerable before expiry, unknown after."""

from __future__ import annotations

from membench.clock import week_date
from membench.schema import AuthorityTier, TypedValue
from membench.templates.base import BuildContext, Template, expect_value, register
from membench.templates.builders_ext import expect_unknown_abstain

TEMPLATE_ID = "t06_expiring_fact"
FAMILY = "temporal"


def build(ctx: BuildContext) -> None:
    org = ctx.entity("organization", "business")
    oname = org.canonical_name
    discount = ctx.rng.randrange(5, 30)
    expiry = week_date(8, 0).isoformat()

    s_offer = ctx.source(
        2,
        f"{oname} partner offer",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"{oname} extends a partner discount of {discount} percent.",
            f"The offer stands until {expiry} and lapses after that date.",
        ],
    )
    c_offer = ctx.claim(
        org,
        "partner_discount",
        TypedValue(kind="quantity", value=str(discount), unit="percent"),
        s_offer,
        valid_to_week=8,
    )

    ctx.query(
        "current_truth",
        f"What partner discount does {oname} currently offer?",
        knowledge_week=5,
        family=FAMILY,
        expect=expect_value(c_offer),
    )
    ctx.query(
        "current_truth",
        f"What partner discount does {oname} offer now that week 8 has passed?",
        knowledge_week=10,
        family=FAMILY,
        expect=expect_unknown_abstain(c_offer),
    )
    ctx.query(
        "as_of",
        f"What partner discount did {oname} offer as of week 4?",
        knowledge_week=10,
        world_week=4,
        family=FAMILY,
        expect=expect_value(c_offer),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Offer with expiry: value before week 8, unknown/abstain afterwards",
        variants=4,
        build=build,
    )
)
