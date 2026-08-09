"""T11 — derived claims chain back to the original source through provenance."""

from __future__ import annotations

from membench import wordbank
from membench.schema import AuthorityTier, TypedValue
from membench.templates.base import (
    BuildContext,
    Template,
    expect_abstain,
    expect_value,
    register,
)

TEMPLATE_ID = "t11_transitive_provenance"
FAMILY = "epistemics"


def build(ctx: BuildContext) -> None:
    instrument = ctx.entity(
        "product", "science", name=wordbank.product_name(ctx.rng)
    )
    iname = instrument.canonical_name
    units = ctx.rng.randrange(300, 900)

    s_lab = ctx.source(
        1,
        f"{iname} lab log",
        authority=AuthorityTier.FIRSTHAND,
        lines=[f"The {iname} instrument recorded {units} units in its first pass."],
    )
    c_original = ctx.claim(
        instrument,
        "recorded_output",
        TypedValue(kind="quantity", value=str(units), unit="units"),
        s_lab,
    )

    s_digest = ctx.source(
        3,
        f"{iname} weekly digest",
        authority=AuthorityTier.SECONDHAND,
        lines=[f"The weekly digest carries the {iname} reading of {units} units."],
    )
    c_digest = ctx.claim(
        instrument,
        "digest_output",
        TypedValue(kind="quantity", value=str(units), unit="units"),
        s_digest,
        derived_from=[c_original.claim_id],
    )

    s_brief = ctx.source(
        5,
        f"{iname} planning brief",
        authority=AuthorityTier.OFFICIAL,
        lines=[f"Planning assumes the {iname} output of {units} units."],
    )
    c_planning = ctx.claim(
        instrument,
        "planning_output",
        TypedValue(kind="quantity", value=str(units), unit="units"),
        s_brief,
        derived_from=[c_digest.claim_id],
    )

    ctx.query(
        "current_truth",
        f"What output figure is the {iname} planning brief built on?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_value(c_planning),
    )
    ctx.query(
        "direct_recall",
        f"What did the {iname} lab log itself record?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_value(c_original),
    )
    ctx.query(
        "unanswerable",
        f"Which technician signed the {iname} lab log?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_abstain(),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Derived-from chain: answers must cite back to the original source",
        variants=4,
        build=build,
    )
)
