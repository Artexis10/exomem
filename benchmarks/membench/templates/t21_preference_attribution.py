"""T21 — opinions stay attributed to their holder and as-of position."""

from __future__ import annotations

from membench.ids import slugify
from membench.schema import AuthorityTier, TypedValue
from membench.templates.base import BuildContext, Template, expect_abstain, register
from membench.templates.builders_ext import expect_attributed_opinion

TEMPLATE_ID = "t21_preference_attribution"
FAMILY = "preference_attribution"


def build(ctx: BuildContext) -> None:
    holder = ctx.entity("person", "business")
    unrecorded_holder = ctx.entity("person", "business")
    project = ctx.entity("project", "business")
    metric = ctx.metric()
    predicate = f"position_on_{slugify(metric).replace('-', '_')}"
    old_target = ctx.rng.randrange(24, 48)
    new_target = old_target + ctx.rng.randrange(4, 12)
    observed = new_target + ctx.rng.randrange(3, 9)
    old_position = f"{holder.canonical_name} favors keeping the {metric} below {old_target} points"
    new_position = f"{holder.canonical_name} favors keeping the {metric} below {new_target} points"
    objective_value = f"{project.canonical_name} measured {observed} points on the {metric}"

    s_opinion_v1 = ctx.source(
        1,
        f"Interview with {holder.canonical_name}",
        authority=AuthorityTier.FIRSTHAND,
        lines=[f'Direct quote: "{old_position}."'],
    )
    c_opinion_v1 = ctx.claim(
        holder,
        predicate,
        TypedValue(kind="text", value=old_position),
        s_opinion_v1,
    )

    s_objective = ctx.source(
        2,
        f"{project.canonical_name} measurement register",
        authority=AuthorityTier.OFFICIAL,
        lines=[f"Official reading: {objective_value}."],
    )
    c_objective = ctx.claim(
        project,
        "observed_level",
        TypedValue(kind="text", value=objective_value),
        s_objective,
    )

    s_opinion_v2 = ctx.source(
        6,
        f"Follow-up interview with {holder.canonical_name}",
        authority=AuthorityTier.FIRSTHAND,
        lines=[f'{holder.canonical_name} changed position and said: "{new_position}."'],
    )
    c_opinion_v2 = ctx.claim(
        holder,
        predicate,
        TypedValue(kind="text", value=new_position),
        s_opinion_v2,
    )
    ctx.supersede(c_opinion_v1, c_opinion_v2, week=6)

    ctx.query(
        "holder_opinion",
        f"What does {holder.canonical_name} think about the {metric}?",
        knowledge_week=4,
        family=FAMILY,
        expect=expect_attributed_opinion(c_opinion_v1, holder, c_objective),
    )
    ctx.query(
        "opinion_as_of",
        f"As of week 8, what does {holder.canonical_name} think about the {metric}?",
        knowledge_week=9,
        world_week=8,
        family=FAMILY,
        expect=expect_attributed_opinion(
            c_opinion_v2,
            holder,
            c_objective,
            forbidden=[c_opinion_v1],
        ),
    )
    ctx.query(
        "opinion_objectivity",
        f"Is keeping the {metric} below {old_target} points objectively best for "
        f"{project.canonical_name}?",
        knowledge_week=4,
        family=FAMILY,
        expect=expect_attributed_opinion(c_opinion_v1, holder, c_objective, hedged=True),
    )
    ctx.query(
        "unrecorded_opinion",
        f"What does {unrecorded_holder.canonical_name} think about the {metric}?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_abstain(),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Holder-bound opinions: changed position, objective contrast, abstention",
        variants=4,
        build=build,
    )
)
