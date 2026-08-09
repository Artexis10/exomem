"""T04 — late-arriving disproof rewrites history retroactively."""

from __future__ import annotations

from membench.schema import AuthorityTier, TypedValue
from membench.templates.base import BuildContext, Template, expect_value, register
from membench.templates.builders_ext import expect_disproved

TEMPLATE_ID = "t04_late_evidence"
FAMILY = "temporal"


def build(ctx: BuildContext) -> None:
    project = ctx.entity("project", "science")
    pname = project.canonical_name
    reading = ctx.rng.randrange(300, 900)

    s_field = ctx.source(
        1,
        f"{pname} bench readout",
        authority=AuthorityTier.FIRSTHAND,
        lines=[f"The bench readout for {pname} came in at {reading} units."],
    )
    c_reading = ctx.claim(
        project,
        "bench_readout",
        TypedValue(kind="quantity", value=str(reading), unit="units"),
        s_field,
    )

    s_audit = ctx.source(
        6,
        f"{pname} calibration audit",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"The calibration audit found every {pname} bench readout invalid.",
            f"A sensor fault corrupted the {pname} bench from its first week onward.",
        ],
    )
    ctx.disprove(c_reading, s_audit, week=6, retroactive_week=1)

    ctx.query(
        "as_of",
        f"What was the bench readout for {pname} as of week 2?",
        knowledge_week=4,
        world_week=2,
        family=FAMILY,
        expect=expect_value(c_reading),
    )
    ctx.query(
        "as_of",
        f"Given everything now known, what was the valid bench readout for {pname} "
        "as of week 2?",
        knowledge_week=9,
        world_week=2,
        family=FAMILY,
        expect=expect_disproved(c_reading),
    )
    ctx.query(
        "current_truth",
        f"What is the accepted bench readout for {pname} today?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_disproved(c_reading),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Retroactive disproof: same world week answered differently by knowledge week",
        variants=4,
        build=build,
    )
)
