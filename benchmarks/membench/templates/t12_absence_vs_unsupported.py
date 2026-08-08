"""T12 — evidence of absence is answerable; unsupported plausibilities are not."""

from __future__ import annotations

from membench.schema import AuthorityTier, TypedValue
from membench.templates.base import (
    BuildContext,
    Template,
    expect_abstain,
    expect_value,
    register,
)

TEMPLATE_ID = "t12_absence_vs_unsupported"
FAMILY = "epistemics"


def build(ctx: BuildContext) -> None:
    project = ctx.entity("project", "operations")
    pname = project.canonical_name
    scope = ctx.rng.randrange(9, 40)

    s_audit = ctx.source(
        4,
        f"{pname} quarterly audit",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"The quarterly audit for {pname} covered {scope} services end to end.",
            f"Critical findings for {pname} this quarter: none found.",
        ],
    )
    c_absence = ctx.claim(
        project,
        "critical_findings",
        TypedValue(kind="text", value="none found"),
        s_audit,
    )
    c_scope = ctx.claim(
        project,
        "audit_scope",
        TypedValue(kind="quantity", value=str(scope), unit="services"),
        s_audit,
    )

    ctx.query(
        "current_truth",
        f"Were any critical findings reported in the {pname} quarterly audit?",
        knowledge_week=7,
        family=FAMILY,
        expect=expect_value(c_absence),
    )
    ctx.query(
        "unanswerable",
        f"What fine amount did the {pname} quarterly audit assess?",
        knowledge_week=7,
        family="query_behavior",
        expect=expect_abstain(),
    )
    ctx.query(
        "direct_recall",
        f"How many services did the {pname} quarterly audit cover?",
        knowledge_week=7,
        family="query_behavior",
        expect=expect_value(c_scope),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Recorded absence answered; plausible-but-unsupported fact abstained",
        variants=4,
        build=build,
    )
)
