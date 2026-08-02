"""T20 — cross-lingual facts: Cyrillic sources queried through Latin aliases.

Each variant gives one entity a native-script canonical name and a Latin alias.
Two native-script markdown sources record a stable count and a superseded count;
English queries must recover the same value and sentinel citation as a native-
script control, while an unrecorded count still requires abstention.
"""

from __future__ import annotations

from membench import wordbank
from membench.schema import AuthorityTier, TypedValue
from membench.templates.base import (
    BuildContext,
    Template,
    expect_abstain,
    expect_value,
    register,
)

TEMPLATE_ID = "t20_cross_lingual"
FAMILY = "cross_lingual"


def build(ctx: BuildContext) -> None:
    native_name, latin_alias = wordbank.org_name_cyr(
        ctx.rng, discriminator=ctx.variant
    )
    org = ctx.entity(
        "organization",
        "business",
        aliases=[latin_alias],
        name=native_name,
    )
    archive_count = ctx.rng.randrange(40, 90)
    routing_count_old = ctx.rng.randrange(10, 40)
    routing_count_new = routing_count_old + ctx.rng.randrange(5, 20)

    initial = ctx.source(
        2,
        f"{native_name} начальная ведомость",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"Для организации {native_name} архивное число равно {archive_count} единиц.",
            f"Для организации {native_name} маршрутное число равно "
            f"{routing_count_old} единиц.",
        ],
    )
    archive_claim = ctx.claim(
        org,
        "archive_count",
        TypedValue(kind="quantity", value=str(archive_count), unit="units"),
        initial,
    )
    routing_old = ctx.claim(
        org,
        "routing_count",
        TypedValue(kind="quantity", value=str(routing_count_old), unit="units"),
        initial,
    )

    revision = ctx.source(
        6,
        f"{native_name} новая ведомость",
        authority=AuthorityTier.OFFICIAL,
        lines=[
            f"Для организации {native_name} маршрутное число теперь равно "
            f"{routing_count_new} единиц.",
            "Прежнее маршрутное число отменено.",
        ],
    )
    routing_new = ctx.claim(
        org,
        "routing_count",
        TypedValue(kind="quantity", value=str(routing_count_new), unit="units"),
        revision,
    )
    ctx.supersede(routing_old, routing_new, week=6)

    ctx.query(
        "cross_script_direct_recall",
        f"What archive count is recorded for {latin_alias}?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_value(archive_claim),
    )
    ctx.query(
        "cross_script_current_truth",
        f"What is the current routing count for {latin_alias}?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_value(routing_new, forbidden=[routing_old]),
    )
    ctx.query(
        "unanswerable",
        f"What inspection count is recorded for {latin_alias}?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_abstain(),
    )
    ctx.query(
        "same_script_control",
        f"Каково архивное число для организации {native_name}?",
        knowledge_week=9,
        family=FAMILY,
        expect=expect_value(archive_claim),
    )


register(
    Template(
        template_id=TEMPLATE_ID,
        family=FAMILY,
        summary=(
            "Cyrillic sources queried through Latin aliases: direct recall, "
            "supersession, abstention, and same-script control"
        ),
        variants=4,
        build=build,
    )
)
