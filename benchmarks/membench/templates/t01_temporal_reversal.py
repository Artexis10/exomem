"""T01 — a decision later fully reversed: current truth, as-of, what changed."""

from __future__ import annotations

from membench.schema import AuthorityTier, TypedValue
from membench.templates.base import (
    BuildContext,
    Template,
    expect_superseded_history,
    expect_value,
    register,
)
from membench.templates.builders_ext import expect_change

TEMPLATE_ID = "t01_temporal_reversal"
FAMILY = "temporal"


def build(ctx: BuildContext) -> None:
    project = ctx.entity("project", "technology")
    pname = project.canonical_name
    provider_a = ctx.org()
    provider_b = ctx.org()
    while provider_b == provider_a:
        provider_b = ctx.org()
    ctx.entity("organization", "business", name=provider_a)
    ctx.entity("organization", "business", name=provider_b)

    s_decide = ctx.source(
        1,
        f"{pname} hosting decision",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"After a full review, {pname} will run on the {provider_a} platform.",
            f"The hosting provider for {pname} is {provider_a}.",
        ],
    )
    c_v1 = ctx.claim(
        project,
        "hosting_provider",
        TypedValue(kind="entity_ref", value=provider_a),
        s_decide,
    )

    s_reverse = ctx.source(
        6,
        f"{pname} hosting reversal memo",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"The earlier hosting decision for {pname} is fully reversed.",
            f"The hosting provider for {pname} is now {provider_b}.",
            f"Rising costs at {provider_a} drove the reversal.",
        ],
    )
    c_v2 = ctx.claim(
        project,
        "hosting_provider",
        TypedValue(kind="entity_ref", value=provider_b),
        s_reverse,
    )
    ctx.supersede(c_v1, c_v2, week=6)

    ctx.query(
        "current_truth",
        f"Which provider currently hosts {pname}?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_value(c_v2, forbidden=[c_v1]),
    )
    ctx.query(
        "as_of",
        f"Which provider hosted {pname} as of week 3, before any reversal?",
        knowledge_week=9,
        world_week=3,
        family=FAMILY,
        expect=expect_superseded_history(c_v1, c_v2),
    )
    ctx.query(
        "what_changed",
        f"How did the hosting decision for {pname} change over time, and why?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_change(c_v1, c_v2, s_reverse),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Decision fully reversed later: current truth, as-of, change-and-why",
        variants=4,
        build=build,
    )
)
