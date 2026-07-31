"""T00 — micro smoke template: 3 sources, one supersession, canary, control.

The CI exemplar every family template follows: entities and values come from
the seeded context, lifecycle transitions use the declarative helpers, and
every query's expectation is oracle-derived.
"""

from __future__ import annotations

from membench.clock import week_date
from membench.ids import slugify
from membench.schema import (
    ArtifactKind,
    AuthorityTier,
    ExpectedAnswer,
    ExpectedRecord,
    TypedValue,
)
from membench.templates.base import (
    BuildContext,
    OracleCtx,
    Template,
    expect_abstain,
    expect_superseded_history,
    expect_value,
    register,
)

TEMPLATE_ID = "t00_mini_smoke"


def _expect_none_needed(ctx: OracleCtx, query) -> ExpectedRecord:  # type: ignore[no-untyped-def]
    return ExpectedRecord(
        query_id=query.query_id,
        answer=ExpectedAnswer(kind="none"),
        gates=["non_activation"],
    )


def build(ctx: BuildContext) -> None:
    project = ctx.entity("project", "technology")
    org = ctx.entity("organization", "business")
    pname = project.canonical_name
    metric = ctx.metric()

    deadline_v1 = week_date(9, ctx.rng.randrange(0, 5)).isoformat()
    deadline_v2 = week_date(11, ctx.rng.randrange(0, 5)).isoformat()
    reading = f"{ctx.rng.randrange(20, 90)}.{ctx.rng.randrange(1, 9)}"

    s_kickoff = ctx.source(
        0,
        f"{pname} kickoff brief",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"{org.canonical_name} approved the kickoff of {pname}.",
            f"The delivery deadline for {pname} is {deadline_v1}.",
        ],
    )
    c_deadline_v1 = ctx.claim(
        project,
        "delivery_deadline",
        TypedValue(kind="date", value=deadline_v1),
        s_kickoff,
    )

    s_field = ctx.source(
        1,
        f"{pname} field report",
        authority=AuthorityTier.FIRSTHAND,
        kind=ArtifactKind.MARKDOWN,
        lines=[f"Field check: the {metric} for {pname} measured {reading} points."],
    )
    c_metric = ctx.claim(
        project,
        slugify(metric),
        TypedValue(kind="quantity", value=reading, unit="points"),
        s_field,
    )

    s_replan = ctx.source(
        4,
        f"{pname} replan memo",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"The delivery deadline for {pname} moved to {deadline_v2}.",
            "Scope and ownership are unchanged.",
        ],
    )
    c_deadline_v2 = ctx.claim(
        project,
        "delivery_deadline",
        TypedValue(kind="date", value=deadline_v2),
        s_replan,
    )
    ctx.supersede(c_deadline_v1, c_deadline_v2, week=4)

    ctx.snapshot(3)
    ctx.snapshot(7)
    ctx.snapshot(11)

    ctx.query(
        "current_truth",
        f"What is the current delivery deadline for {pname}?",
        knowledge_week=8,
        expect=expect_value(c_deadline_v2, forbidden=[c_deadline_v1]),
    )
    ctx.query(
        "as_of",
        f"What was the delivery deadline for {pname} as of week 2, before any replanning?",
        knowledge_week=8,
        world_week=2,
        expect=expect_superseded_history(c_deadline_v1, c_deadline_v2),
    )
    ctx.query(
        "direct_recall",
        f"How many points did the {metric} for {pname} measure in the field report?",
        knowledge_week=8,
        expect=expect_value(c_metric),
        canary=True,
    )
    ctx.query(
        "unanswerable",
        f"Which vendor performed the security audit for {pname}?",
        knowledge_week=8,
        expect=expect_abstain(),
    )
    ctx.query(
        "no_memory_needed",
        "Thanks, that looks good - go ahead.",
        knowledge_week=8,
        should_activate=False,
        tracks=["C"],
        modes=["agent"],
        expect=_expect_none_needed,
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family="query_behavior",
        summary="Micro smoke scenario: supersession, as-of, canary recall, abstention, control",
        variants=4,
        build=build,
    )
)
