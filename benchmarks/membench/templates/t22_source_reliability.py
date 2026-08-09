"""T22 — a recurring bulletin corrects itself twice before a fresh claim.

``supersedes_source`` topology: a source-level supersession edge means *this
edition replaces that edition*, so it is only authored together with
claim-level supersession that retires the replaced edition's claims — the
meaning t13 already uses for its digest refresh, and the meaning the two
correction editions below carry (v2 retires c_v1, v3 retires c_v2). The later
``fresh`` issue therefore declares no such edge and stays at version 1: it adds
a new unconfirmed claim about a different metric while v3's claim is still
current, and v3 is still the required citation for the current-metric answer.
Declaring supersession there would have made ``expected.jsonl`` tell a memory
system to cite an edition the corpus had told it to retire, penalising exactly
the behaviour source supersession is supposed to reward.
"""

from __future__ import annotations

from membench.ids import slugify
from membench.schema import AuthorityTier, ClaimStatus, ScheduleOpKind, TypedValue
from membench.templates.base import BuildContext, Template, expect_value, register
from membench.templates.builders_ext import (
    expect_correction_history,
    expect_settled_value,
    expect_value_with_correction_history,
)

TEMPLATE_ID = "t22_source_reliability"
FAMILY = "source_reliability"


def build(ctx: BuildContext) -> None:
    bulletin = ctx.entity("organization", "business")
    clean_org = ctx.entity("organization", "business")
    project = ctx.entity("project", "operations")
    metric = ctx.metric()
    clean_metric = ctx.metric()
    while clean_metric == metric:
        clean_metric = ctx.metric()
    fresh_metric = ctx.metric()
    while fresh_metric in {metric, clean_metric}:
        fresh_metric = ctx.metric()

    value_v1 = ctx.rng.randrange(110, 180)
    value_v2 = value_v1 + ctx.rng.randrange(8, 24)
    value_v3 = value_v2 - ctx.rng.randrange(3, 8)
    clean_value = ctx.rng.randrange(40, 90)
    fresh_value = ctx.rng.randrange(70, 130)
    source_title = f"{bulletin.canonical_name} weekly bulletin"
    predicate = slugify(metric).replace("-", "_")

    s_v1 = ctx.source(
        1,
        source_title,
        authority=AuthorityTier.OFFICIAL,
        version=1,
        lines=[
            f"{bulletin.canonical_name} reports the {metric} for "
            f"{project.canonical_name} at {value_v1} points."
        ],
    )
    c_v1 = ctx.claim(
        project,
        predicate,
        TypedValue(kind="quantity", value=str(value_v1), unit="points"),
        s_v1,
    )

    s_v2 = ctx.source(
        4,
        source_title,
        authority=AuthorityTier.OFFICIAL,
        supersedes_source=s_v1.source_id,
        version=2,
        schedule_op=ScheduleOpKind.CORRECT_SOURCE,
        lines=[
            f"{bulletin.canonical_name} corrects its earlier {metric} value.",
            f"The corrected {metric} for {project.canonical_name} is {value_v2} points.",
        ],
    )
    c_v2 = ctx.claim(
        project,
        predicate,
        TypedValue(kind="quantity", value=str(value_v2), unit="points"),
        s_v2,
    )
    ctx.supersede(c_v1, c_v2, week=4)

    s_v3 = ctx.source(
        7,
        source_title,
        authority=AuthorityTier.OFFICIAL,
        supersedes_source=s_v2.source_id,
        version=3,
        schedule_op=ScheduleOpKind.CORRECT_SOURCE,
        lines=[
            f"{bulletin.canonical_name} issues a second correction to the {metric}.",
            f"The latest corrected {metric} for {project.canonical_name} is {value_v3} points.",
        ],
    )
    c_v3 = ctx.claim(
        project,
        predicate,
        TypedValue(kind="quantity", value=str(value_v3), unit="points"),
        s_v3,
    )
    ctx.supersede(c_v2, c_v3, week=7)

    s_clean = ctx.source(
        5,
        f"{clean_org.canonical_name} metric register",
        authority=AuthorityTier.SYSTEM_OF_RECORD,
        lines=[
            f"{clean_org.canonical_name} records the {clean_metric} for "
            f"{project.canonical_name} at {clean_value} points."
        ],
    )
    c_clean = ctx.claim(
        project,
        slugify(clean_metric).replace("-", "_"),
        TypedValue(kind="quantity", value=str(clean_value), unit="points"),
        s_clean,
    )

    # A later issue of the same recurring bulletin: it neither replaces nor
    # retires the corrected edition (c_v3 stays current), so it opens no
    # supersession edge and starts its own version lineage at 1. Its link to
    # the correction record is publication identity — the shared title.
    s_fresh = ctx.source(
        9,
        source_title,
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"A fresh unconfirmed {fresh_metric} value for {project.canonical_name} "
            f"is {fresh_value} points."
        ],
    )
    c_fresh = ctx.claim(
        project,
        slugify(fresh_metric).replace("-", "_"),
        TypedValue(kind="quantity", value=str(fresh_value), unit="points"),
        s_fresh,
        status=ClaimStatus.TENTATIVE,
    )

    ctx.query(
        "current_corrected_metric",
        f"What is the current {metric} for {project.canonical_name}?",
        knowledge_week=10,
        family=FAMILY,
        expect=expect_value(c_v3, forbidden=[c_v1, c_v2]),
    )
    ctx.query(
        "correction_history",
        f"Has {bulletin.canonical_name} issued corrections to the {metric}?",
        knowledge_week=10,
        family=FAMILY,
        expect=expect_correction_history(c_v1),
    )
    ctx.query(
        "clean_metric",
        f"What {clean_metric} does {clean_org.canonical_name} record for {project.canonical_name}?",
        knowledge_week=10,
        family=FAMILY,
        # The discriminating calibration axis: a system-of-record fact with no
        # correction record must be stated plainly, so a provider that hedges
        # every answer fails here instead of hedging its way past the family.
        expect=expect_settled_value(c_clean),
    )
    ctx.query(
        "fresh_unconfirmed",
        f"What fresh {fresh_metric} value does {bulletin.canonical_name} give for "
        f"{project.canonical_name}?",
        knowledge_week=10,
        family=FAMILY,
        expect=expect_value_with_correction_history(c_fresh, c_v1),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Recurring bulletin: two corrections, clean contrast, fresh hedging",
        variants=4,
        build=build,
    )
)
