"""T13 — duplicate/reworded ingestion, a stale summary refresh, and a deletion."""

from __future__ import annotations

from membench.ids import slugify
from membench.schema import AuthorityTier, ScheduleOp, ScheduleOpKind, TypedValue
from membench.templates.base import (
    BuildContext,
    Template,
    expect_superseded_history,
    expect_value,
    register,
)

TEMPLATE_ID = "t13_entropy_dedup"
FAMILY = "maintenance"


def build(ctx: BuildContext) -> None:
    project = ctx.entity("project", "operations")
    pname = project.canonical_name
    retention = ctx.rng.randrange(30, 120)
    metric = ctx.metric()
    value_old = ctx.rng.randrange(200, 600)
    value_new = value_old + ctx.rng.randrange(20, 80)

    policy_lines = [f"The records policy for {pname} sets retention at {retention} days."]
    s_policy = ctx.source(
        1,
        f"{pname} records policy note",
        authority=AuthorityTier.OFFICIAL,
        lines=policy_lines,
    )
    c_retention = ctx.claim(
        project,
        "records_retention",
        TypedValue(kind="quantity", value=str(retention), unit="days"),
        s_policy,
    )
    # Verbatim duplicate of the policy note, ingested as a duplicate op.
    ctx.source(
        2,
        f"{pname} records policy note",
        authority=AuthorityTier.OFFICIAL,
        lines=list(policy_lines),
        schedule_op=ScheduleOpKind.DUPLICATE_SOURCE,
    )
    # Reworded third ingestion of the same fact corroborates the claim.
    s_reword = ctx.source(
        3,
        f"{pname} retention reminder",
        authority=AuthorityTier.FIRSTHAND,
        lines=[f"Reminder: {pname} keeps its records for {retention} days."],
    )
    ctx.confirm(c_retention, s_reword, week=3)

    s_stale = ctx.source(
        2,
        f"{pname} status digest",
        authority=AuthorityTier.SECONDHAND,
        lines=[f"The digest pegs the {metric} for {pname} at {value_old}."],
    )
    c_metric_old = ctx.claim(
        project,
        slugify(metric),
        TypedValue(kind="quantity", value=str(value_old), unit="points"),
        s_stale,
    )
    s_fresh = ctx.source(
        6,
        f"{pname} status digest refresh",
        authority=AuthorityTier.SECONDHAND,
        lines=[
            "The refreshed digest replaces the earlier edition.",
            f"The {metric} for {pname} now reads {value_new}.",
        ],
        supersedes_source=s_stale.source_id,
        version=2,
    )
    c_metric_new = ctx.claim(
        project,
        slugify(metric),
        TypedValue(kind="quantity", value=str(value_new), unit="points"),
        s_fresh,
    )
    ctx.supersede(c_metric_old, c_metric_new, week=6)

    s_noise = ctx.source(
        3,
        f"{pname} scratch jottings",
        authority=AuthorityTier.RUMOR,
        lines=["Loose hallway jottings; nothing here is verified."],
    )
    ctx.graph.schedule.append(
        ScheduleOp(
            week=8,
            seq=len(ctx.graph.schedule),
            op=ScheduleOpKind.DELETE_SOURCE,
            source_id=s_noise.source_id,
        )
    )

    ctx.snapshot(3)
    ctx.snapshot(7)
    ctx.snapshot(11)

    ctx.query(
        "current_truth",
        f"How many days does {pname} retain its records?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_value(c_retention),
    )
    ctx.query(
        "current_truth",
        f"What does the {metric} for {pname} read now?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_value(c_metric_new, forbidden=[c_metric_old]),
    )
    ctx.query(
        "as_of",
        f"What did the {metric} for {pname} read as of week 4?",
        knowledge_week=9,
        world_week=4,
        family=FAMILY,
        expect=expect_superseded_history(c_metric_old, c_metric_new),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Triple ingestion with duplicate op, digest refresh, noise deletion",
        variants=4,
        build=build,
    )
)
