"""T14 — same-name people, a project rename, a role change, and a multi-hop ask."""

from __future__ import annotations

from membench import wordbank
from membench.clock import week_date
from membench.schema import AuthorityTier, NameSpan, TypedValue
from membench.templates.base import (
    BuildContext,
    Template,
    expect_abstain,
    expect_value,
    register,
)

TEMPLATE_ID = "t14_identity_graph"
FAMILY = "identity"


def build(ctx: BuildContext) -> None:
    shared_name = ctx.person()
    org_a = ctx.entity("organization", "business")
    org_b_name = ctx.org()
    while org_b_name == org_a.canonical_name:
        org_b_name = ctx.org()
    org_b = ctx.entity("organization", "business", name=org_b_name)
    person_a = ctx.entity("person", "business", name=shared_name)
    person_b = ctx.entity("person", "business", name=shared_name)

    old_project_name = ctx.project()
    new_project_name = ctx.project()
    if new_project_name == old_project_name:
        new_project_name = f"{old_project_name} II"
    project = ctx.entity(
        "project", "technology", name=new_project_name, aliases=[old_project_name]
    )
    project.name_timeline = [
        NameSpan(
            name=old_project_name,
            valid_from=week_date(0, 0),
            valid_to=week_date(5, 0),
        ),
        NameSpan(name=new_project_name, valid_from=week_date(5, 0)),
    ]
    component = ctx.entity(
        "product", "technology", name=wordbank.product_name(ctx.rng)
    )
    leader_name = ctx.person()
    while leader_name == shared_name:
        leader_name = ctx.person()
    ctx.entity("person", "business", name=leader_name)

    s_dir_a = ctx.source(
        1,
        f"{org_a.canonical_name} directory extract",
        authority=AuthorityTier.OFFICIAL,
        lines=[f"{shared_name} serves as operations coordinator at {org_a.canonical_name}."],
    )
    c_role_a_v1 = ctx.claim(
        person_a,
        "role",
        TypedValue(kind="text", value="operations coordinator"),
        s_dir_a,
    )
    s_dir_b = ctx.source(
        1,
        f"{org_b.canonical_name} directory extract",
        authority=AuthorityTier.OFFICIAL,
        lines=[f"{shared_name} works as finance analyst at {org_b.canonical_name}."],
    )
    ctx.claim(
        person_b,
        "role",
        TypedValue(kind="text", value="finance analyst"),
        s_dir_b,
    )

    s_kickoff = ctx.source(
        0,
        f"{old_project_name} kickoff note",
        authority=AuthorityTier.OFFICIAL,
        lines=[f"{old_project_name} kicked off under {org_a.canonical_name}."],
    )
    c_name_v1 = ctx.claim(
        project,
        "official_name",
        TypedValue(kind="text", value=old_project_name),
        s_kickoff,
    )
    s_owns = ctx.source(
        2,
        f"{component.canonical_name} ownership record",
        authority=AuthorityTier.OFFICIAL,
        lines=[f"{component.canonical_name} is owned by {old_project_name}."],
    )
    c_owns = ctx.claim(
        component,
        "owning_project",
        TypedValue(kind="entity_ref", value=old_project_name),
        s_owns,
    )
    s_lead = ctx.source(
        3,
        f"{old_project_name} leadership note",
        authority=AuthorityTier.OFFICIAL,
        lines=[f"{leader_name} leads {old_project_name} day to day."],
    )
    c_lead = ctx.claim(
        project,
        "project_lead",
        TypedValue(kind="entity_ref", value=leader_name),
        s_lead,
        derived_from=[c_owns.claim_id],
    )

    s_rename = ctx.source(
        5,
        f"{old_project_name} rename memo",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"{old_project_name} is renamed to {new_project_name}.",
            f"All artifacts now use the {new_project_name} name.",
        ],
    )
    c_name_v2 = ctx.claim(
        project,
        "official_name",
        TypedValue(kind="text", value=new_project_name),
        s_rename,
    )
    ctx.supersede(c_name_v1, c_name_v2, week=5)

    s_promote = ctx.source(
        6,
        f"{org_a.canonical_name} promotion notice",
        authority=AuthorityTier.OFFICIAL,
        lines=[f"{org_a.canonical_name} promoted {shared_name} to operations director."],
    )
    c_role_a_v2 = ctx.claim(
        person_a,
        "role",
        TypedValue(kind="text", value="operations director"),
        s_promote,
    )
    ctx.supersede(c_role_a_v1, c_role_a_v2, week=6)

    ctx.query(
        "clarify",
        f"What is {shared_name}'s current role?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_abstain(clarify=True),
    )
    ctx.query(
        "multi_hop",
        f"Who leads the project that owns {component.canonical_name}?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_value(c_lead),
    )
    ctx.query(
        "current_truth",
        f"What is the current official name of the project once called "
        f"{old_project_name}?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_value(c_name_v2, forbidden=[c_name_v1]),
    )
    ctx.query(
        "current_truth",
        f"What is {shared_name}'s current role at {org_a.canonical_name}?",
        knowledge_week=8,
        family=FAMILY,
        expect=expect_value(c_role_a_v2, forbidden=[c_role_a_v1]),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary="Shared names need clarification; renames and role changes stay tracked",
        variants=4,
        build=build,
    )
)
