"""T15 — values living in CSV tables, PNG charts, unit pairs, and weekly trends."""

from __future__ import annotations

from membench.ids import slugify
from membench.schema import ArtifactKind, AuthorityTier, TypedValue
from membench.templates.base import (
    BuildContext,
    Template,
    expect_superseded_history,
    expect_value,
    register,
)
from membench.templates.builders_ext import expect_converted_value

TEMPLATE_ID = "t15_numeric_multimodal"
FAMILY = "multimodal"


def build(ctx: BuildContext) -> None:
    project = ctx.entity("project", "operations")
    pname = project.canonical_name
    chart_metric = ctx.metric()
    trend_metric = ctx.metric()
    while trend_metric == chart_metric:
        trend_metric = ctx.metric()

    # Value present only in a CSV table.
    capacity = ctx.rng.randrange(120, 900)
    s_csv = ctx.source(
        2,
        f"{pname} capacity sheet",
        authority=AuthorityTier.OFFICIAL,
        kind=ArtifactKind.CSV,
        table=[
            ["measure", "amount", "unit"],
            ["storage capacity", str(capacity), "crates"],
        ],
    )
    c_capacity = ctx.claim(
        project,
        "storage_capacity",
        TypedValue(kind="quantity", value=str(capacity), unit="crates"),
        s_csv,
    )

    # Value present only inside a PNG wall chart (not text-searchable).
    chart_value = ctx.rng.randrange(40, 95)
    prior_value = chart_value - ctx.rng.randrange(5, 15)
    s_png = ctx.source(
        3,
        f"{pname} wall chart",
        authority=AuthorityTier.FIRSTHAND,
        kind=ArtifactKind.PNG,
        lines=[f"Current {chart_metric} for {pname}: {chart_value} points."],
    )
    c_chart = ctx.claim(
        project,
        slugify(chart_metric),
        TypedValue(kind="quantity", value=str(chart_value), unit="points"),
        s_png,
    )
    s_chart_note = ctx.source(
        3,
        f"{pname} chart note",
        authority=AuthorityTier.FIRSTHAND,
        lines=[
            f"A fresh wall chart for {pname} is posted by the stairwell.",
            f"Last month the {chart_metric} for {pname} stood at {prior_value} points.",
        ],
    )
    c_prior = ctx.claim(
        project,
        f"prior-{slugify(chart_metric)}",
        TypedValue(kind="quantity", value=str(prior_value), unit="points"),
        s_chart_note,
    )

    # Unit conversion pair: stated in kg, asked in grams.
    whole = ctx.rng.randrange(2, 9)
    tenth = ctx.rng.randrange(1, 9)
    mass_kg = f"{whole}.{tenth}"
    mass_g = str(whole * 1000 + tenth * 100)
    s_mass = ctx.source(
        4,
        f"{pname} shipment record",
        authority=AuthorityTier.OFFICIAL,
        lines=[f"The {pname} sample shipment weighed {mass_kg} kg on the dock scale."],
    )
    c_mass = ctx.claim(
        project,
        "shipment_mass",
        TypedValue(kind="quantity", value=mass_kg, unit="kg"),
        s_mass,
    )

    # Longitudinal trend across three weekly measurements.
    reading_1 = ctx.rng.randrange(150, 400)
    reading_2 = reading_1 + ctx.rng.randrange(5, 30)
    reading_3 = reading_2 + ctx.rng.randrange(5, 30)
    trend_claims = []
    for week, reading in ((5, reading_1), (6, reading_2), (7, reading_3)):
        s_week = ctx.source(
            week,
            f"{pname} week {week} check",
            authority=AuthorityTier.FIRSTHAND,
            lines=[f"Weekly check: the {trend_metric} for {pname} measured {reading}."],
        )
        trend_claims.append(
            ctx.claim(
                project,
                slugify(trend_metric),
                TypedValue(kind="quantity", value=str(reading), unit="points"),
                s_week,
            )
        )
    c_t1, c_t2, c_t3 = trend_claims
    ctx.supersede(c_t1, c_t2, week=6)
    ctx.supersede(c_t2, c_t3, week=7)

    ctx.query(
        "direct_recall",
        f"What storage capacity is recorded for {pname}?",
        knowledge_week=6,
        family=FAMILY,
        expect=expect_value(c_capacity),
    )
    ctx.query(
        "direct_recall",
        f"What does the wall chart show for the current {chart_metric} of {pname}?",
        knowledge_week=6,
        family=FAMILY,
        tracks=["B"],
        modes=["qa"],
        expect=expect_value(c_chart, forbidden=[c_prior], require_active=True),
    )
    ctx.query(
        "conversion",
        f"How many grams did the {pname} sample shipment weigh?",
        knowledge_week=7,
        family=FAMILY,
        expect=expect_converted_value(c_mass, mass_g, "g"),
    )
    ctx.query(
        "current_truth",
        f"What is the latest weekly {trend_metric} reading for {pname}?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_value(c_t3, forbidden=[c_t1, c_t2]),
    )
    ctx.query(
        "as_of",
        f"What was the weekly {trend_metric} reading for {pname} as of week 5?",
        knowledge_week=9,
        world_week=5,
        family=FAMILY,
        expect=expect_superseded_history(c_t1, c_t2),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="CSV-only and PNG-only values, unit conversion, weekly trend",
        variants=4,
        build=build,
    )
)
