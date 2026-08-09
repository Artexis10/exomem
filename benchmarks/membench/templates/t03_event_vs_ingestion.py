"""T03 — event happened in week 1 but its report was only ingested in week 5."""

from __future__ import annotations

from membench import wordbank
from membench.clock import week_date
from membench.ids import stable_id
from membench.schema import AuthorityTier, EventRecord, TypedValue
from membench.templates.base import BuildContext, Template, expect_value, register
from membench.templates.builders_ext import expect_unknown_abstain

TEMPLATE_ID = "t03_event_vs_ingestion"
FAMILY = "temporal"


def build(ctx: BuildContext) -> None:
    site = ctx.entity(
        "place", "operations", name=f"{wordbank.city_name(ctx.rng)} Depot"
    )
    sname = site.canonical_name
    advisories = ctx.rng.randrange(2, 9)

    s_report = ctx.source(
        5,
        f"{sname} inspection report",
        authority=AuthorityTier.FIRSTHAND,
        lines=[
            f"The site inspection at {sname} took place in mid-January.",
            "The report reached the archive only weeks after the visit.",
            f"The inspection at {sname} logged {advisories} advisories.",
        ],
    )
    # The inspection itself happened in week 1; the report landed in week 5.
    s_report.event_time = week_date(1, 2)
    c_inspection = ctx.claim(
        site,
        "inspection_advisories",
        TypedValue(kind="quantity", value=str(advisories), unit="advisories"),
        s_report,
        week=1,
    )
    ctx.graph.events.append(
        EventRecord(
            event_id=stable_id("EVT", ctx.template_id, str(ctx.variant), "inspection"),
            kind="site_inspection",
            occurred_at=week_date(1, 2),
            recorded_week=5,
            participants=[site.entity_id],
        )
    )

    ctx.query(
        "as_of",
        f"As of week 2, how many advisories had the {sname} inspection logged?",
        knowledge_week=3,
        world_week=2,
        family=FAMILY,
        expect=expect_unknown_abstain(c_inspection),
    )
    ctx.query(
        "as_of",
        f"As of week 2, how many advisories did the {sname} inspection log?",
        knowledge_week=6,
        world_week=2,
        family=FAMILY,
        expect=expect_value(c_inspection),
    )
    ctx.query(
        "current_truth",
        f"How many advisories stand against {sname} from its inspection?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_value(c_inspection),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Week-1 event ingested in week 5: unknown before ingestion, known after",
        variants=4,
        build=build,
    )
)
