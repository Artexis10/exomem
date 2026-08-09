"""T07 — system-of-record and rumor disagree; the register wins, dispute noted."""

from __future__ import annotations

from membench.schema import AuthorityTier, ClaimStatus, TypedValue
from membench.templates.base import BuildContext, Template, expect_value, register
from membench.templates.builders_ext import expect_value_with_dispute

TEMPLATE_ID = "t07_authority_conflict"
FAMILY = "epistemics"


def build(ctx: BuildContext) -> None:
    org = ctx.entity("organization", "business")
    oname = org.canonical_name
    official_count = ctx.rng.randrange(120, 480)
    rumored_count = official_count + ctx.rng.randrange(15, 60)

    s_register = ctx.source(
        2,
        f"{oname} staffing register",
        authority=AuthorityTier.SYSTEM_OF_RECORD,
        lines=[f"The staffing register lists {oname} at {official_count} full-time staff."],
    )
    c_official = ctx.claim(
        org,
        "headcount",
        TypedValue(kind="quantity", value=str(official_count), unit="staff"),
        s_register,
    )

    s_rumor = ctx.source(
        4,
        f"Corridor talk about {oname}",
        authority=AuthorityTier.RUMOR,
        lines=[
            f"Word going around puts {oname} at {rumored_count} full-time staff.",
            "Nobody has produced records to back that figure.",
        ],
    )
    c_rumor = ctx.claim(
        org,
        "headcount",
        TypedValue(kind="quantity", value=str(rumored_count), unit="staff"),
        s_rumor,
        status=ClaimStatus.TENTATIVE,
    )

    ctx.query(
        "current_truth",
        f"How many full-time staff does {oname} have?",
        knowledge_week=7,
        family=FAMILY,
        expect=expect_value_with_dispute(c_official, c_rumor),
    )
    ctx.query(
        "current_truth",
        f"How many full-time staff did {oname} report before any other figures surfaced?",
        knowledge_week=3,
        family=FAMILY,
        expect=expect_value(c_official),
    )
    ctx.query(
        "direct_recall",
        f"What headcount does the {oname} staffing register itself record?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_value_with_dispute(c_official, c_rumor),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Register vs rumor: authoritative value answered, conflict acknowledged",
        variants=4,
        build=build,
    )
)
