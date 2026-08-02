"""T18 — derived quantities: sums, unit conversions, and ratios over stored values.

Every answer requires arithmetic over two stored measurements; the expected
value, unit, and tolerance are computed by the :mod:`membench.quant`
evaluator, with both contributing sources as required citations.

Canary discipline: every number written into artifact text is either an
integer or a decimal with a single-digit integer part, so no artifact can
contain another template's ``NN.N`` canary value under any seed.
"""

from __future__ import annotations

from membench.ids import slugify
from membench.schema import AuthorityTier, TypedValue
from membench.templates.base import (
    BuildContext,
    Template,
    expect_abstain,
    register,
)
from membench.templates.builders_ext import expect_derived_quantity

TEMPLATE_ID = "t18_quantitative"
FAMILY = "quantitative"


def build(ctx: BuildContext) -> None:
    project = ctx.entity("project", "operations")
    pname = project.canonical_name
    metric = ctx.metric()

    # Same-unit sum: two timed drills, both recorded in minutes (integers).
    duration_a = ctx.rng.randrange(25, 95)
    duration_b = ctx.rng.randrange(20, 85)
    s_drill_1 = ctx.source(
        1,
        f"{pname} first drill log",
        authority=AuthorityTier.FIRSTHAND,
        lines=[f"Timed drill: the first {pname} loading drill took {duration_a} min end to end."],
    )
    c_duration_a = ctx.claim(
        project,
        "first_drill_duration",
        TypedValue(kind="quantity", value=str(duration_a), unit="min"),
        s_drill_1,
    )
    s_drill_2 = ctx.source(
        2,
        f"{pname} second drill log",
        authority=AuthorityTier.FIRSTHAND,
        lines=[f"Timed drill: the second {pname} loading drill took {duration_b} min end to end."],
    )
    c_duration_b = ctx.claim(
        project,
        "second_drill_duration",
        TypedValue(kind="quantity", value=str(duration_b), unit="min"),
        s_drill_2,
    )

    # Conversion pair (spec scenario "Derived quantity with units"): one mass
    # stated in kg (single-digit integer part), one in g; asked combined in kg.
    whole = ctx.rng.randrange(2, 7)
    tenth = ctx.rng.randrange(1, 9)
    mass_kg = f"{whole}.{tenth}"
    hundreds = ctx.rng.choice([h for h in range(1, 10) if h != 10 - tenth])
    mass_g = str(hundreds * 100)
    s_crate = ctx.source(
        3,
        f"{pname} crate consignment record",
        authority=AuthorityTier.OFFICIAL,
        lines=[f"Dock record: the {pname} crate consignment weighed {mass_kg} kg on arrival."],
    )
    c_mass_kg = ctx.claim(
        project,
        "crate_consignment_mass",
        TypedValue(kind="quantity", value=mass_kg, unit="kg"),
        s_crate,
    )
    s_pallet = ctx.source(
        4,
        f"{pname} pallet consignment record",
        authority=AuthorityTier.OFFICIAL,
        lines=[f"Dock record: the {pname} pallet consignment weighed {mass_g} g on arrival."],
    )
    c_mass_g = ctx.claim(
        project,
        "pallet_consignment_mass",
        TypedValue(kind="quantity", value=mass_g, unit="g"),
        s_pallet,
    )

    # Ratio/trend pair: week-5 and week-7 readings (distinct predicates keep
    # both current); later = base * k/2 for odd k, so the ratio is exact.
    base_reading = 2 * ctx.rng.randrange(60, 220)
    k = ctx.rng.choice([3, 5, 7])
    later_reading = base_reading * k // 2
    slug = slugify(metric)
    s_week5 = ctx.source(
        5,
        f"{pname} week 5 check",
        authority=AuthorityTier.FIRSTHAND,
        lines=[f"Weekly check: the {metric} for {pname} measured {base_reading} points."],
    )
    c_reading_base = ctx.claim(
        project,
        f"week5-{slug}",
        TypedValue(kind="quantity", value=str(base_reading), unit="points"),
        s_week5,
    )
    s_week7 = ctx.source(
        7,
        f"{pname} week 7 check",
        authority=AuthorityTier.FIRSTHAND,
        lines=[f"Weekly check: the {metric} for {pname} measured {later_reading} points."],
    )
    c_reading_later = ctx.claim(
        project,
        f"week7-{slug}",
        TypedValue(kind="quantity", value=str(later_reading), unit="points"),
        s_week7,
    )

    ctx.query(
        "derived_sum",
        f"How many minutes in total did the two {pname} loading drills take?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_derived_quantity(c_duration_a, c_duration_b, "sum"),
    )
    ctx.query(
        "derived_conversion_sum",
        f"What is the combined mass in kilograms of the {pname} crate and pallet consignments?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_derived_quantity(c_mass_kg, c_mass_g, "sum", unit="kg"),
    )
    ctx.query(
        "derived_ratio",
        f"By what factor did the {metric} for {pname} change from the week 5 reading to the week 7 reading?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_derived_quantity(c_reading_later, c_reading_base, "ratio", places=1),
    )
    ctx.query(
        "unanswerable",
        f"How many kilometres long is the {pname} delivery route?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_abstain(),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Derived quantities: same-unit sum, kg/g conversion sum, weekly ratio, abstention",
        variants=4,
        build=build,
    )
)
