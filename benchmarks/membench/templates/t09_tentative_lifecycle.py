"""T09 — one tentative claim later confirmed, another later disproved."""

from __future__ import annotations

from membench.schema import AuthorityTier, ClaimStatus, TypedValue
from membench.templates.base import BuildContext, Template, expect_value, register
from membench.templates.builders_ext import expect_disproved

TEMPLATE_ID = "t09_tentative_lifecycle"
FAMILY = "epistemics"


def build(ctx: BuildContext) -> None:
    project = ctx.entity("project", "business")
    pname = project.canonical_name
    pilot_value = "approved for the northern site"
    supplier_value = "switching to a single supplier"

    s_hint = ctx.source(
        1,
        f"{pname} pilot early word",
        authority=AuthorityTier.SECONDHAND,
        lines=[
            f"Early word: the {pname} pilot looks approved for the northern site, "
            "pending sign-off."
        ],
    )
    c_pilot = ctx.claim(
        project,
        "pilot_outcome",
        TypedValue(kind="text", value=pilot_value),
        s_hint,
        status=ClaimStatus.TENTATIVE,
    )
    s_confirm = ctx.source(
        5,
        f"{pname} pilot sign-off",
        authority=AuthorityTier.OFFICIAL,
        lines=[f"Sign-off complete: the {pname} pilot is approved for the northern site."],
    )
    ctx.confirm(c_pilot, s_confirm, week=5)

    s_chatter = ctx.source(
        2,
        f"{pname} supplier chatter",
        authority=AuthorityTier.SECONDHAND,
        lines=[f"Chatter suggests {pname} is switching to a single supplier next quarter."],
    )
    c_supplier = ctx.claim(
        project,
        "supplier_strategy",
        TypedValue(kind="text", value=supplier_value),
        s_chatter,
        status=ClaimStatus.TENTATIVE,
    )
    s_deny = ctx.source(
        7,
        f"{pname} procurement statement",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"Procurement confirmed {pname} keeps its full supplier roster.",
            "The single-supplier story is wrong.",
        ],
    )
    ctx.disprove(c_supplier, s_deny, week=7)

    ctx.query(
        "current_truth",
        f"What is the standing of the {pname} pilot?",
        knowledge_week=3,
        family=FAMILY,
        expect=expect_value(c_pilot, hedged=True),
    )
    ctx.query(
        "current_truth",
        f"What is the confirmed standing of the {pname} pilot?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_value(c_pilot),
    )
    ctx.query(
        "current_truth",
        f"Is {pname} changing its supplier setup?",
        knowledge_week=4,
        family=FAMILY,
        expect=expect_value(c_supplier, hedged=True),
    )
    ctx.query(
        "current_truth",
        f"Is {pname} moving to a single supplier?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_disproved(c_supplier),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Tentative claims diverge: one confirmed, one disproved, answers track it",
        variants=4,
        build=build,
    )
)
