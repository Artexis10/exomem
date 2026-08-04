"""T20 — cross-lingual facts: Cyrillic sources queried through Latin aliases.

Each variant gives one entity a native-script canonical name and a Latin alias.
Two native-script markdown sources record a stable count and a superseded count;
English queries must recover the same value and sentinel citation as a native-
script control, while an unrecorded count still requires abstention.
"""

from __future__ import annotations

from membench import wordbank
from membench.ids import stable_id
from membench.schema import AuthorityTier, TypedValue
from membench.templates.base import (
    BuildContext,
    GenerationError,
    Template,
    expect_abstain,
    expect_value,
    register,
)

TEMPLATE_ID = "t20_cross_lingual"
FAMILY = "cross_lingual"
VARIANTS = 4

# Quantity ranges are disjoint by construction. Both counts are "units" of the
# same organisation, so if their ranges overlapped a contender that answered
# with the wrong metric could land on the right number and score as correct —
# a scoring hole, not a memory result. ``routing_count_new`` peaks at the old
# ceiling plus the largest increment; archive counts start strictly above that.
#
# Disjointness alone is not enough: ``gate_state`` tests forbidden values by
# substring, so a three-digit archive count can *contain* a two-digit routing
# count (archive 105 spells routing 10). A correct answer that also mentioned
# the archive figure would then fail with "forbidden value '10' present" — the
# harness punishing correct behaviour, which is the worst defect class here.
# Capping archive at two digits removes the channel: no two distinct two-digit
# numbers are substrings of one another.
_ROUTING_OLD_RANGE = (10, 40)  # randrange bounds: upper is exclusive
_ROUTING_STEP_RANGE = (5, 20)
_ROUTING_CEILING = (_ROUTING_OLD_RANGE[1] - 1) + (_ROUTING_STEP_RANGE[1] - 1)
_ARCHIVE_RANGE = (_ROUTING_CEILING + 1, 100)


def _name_slot(native_name: str) -> int:
    """Which variant owns ``native_name``, from the repo's stable digest.

    ``stable_id`` ends in eight hex digits of a SHA-256 over its parts, so this
    partitions the Cyrillic organisation-name space into ``VARIANTS`` disjoint
    classes that are identical on every machine and every run.
    """

    return int(stable_id("T20ORGSLOT", native_name)[-8:], 16) % VARIANTS


def _variant_org_name(ctx: BuildContext) -> tuple[str, str]:
    """A native/Latin organisation pair no sibling variant can also draw.

    Redraw-until-distinct, the idiom ``t19`` uses for its sibling product name.
    The earlier scheme appended a per-variant marker to a freely drawn name,
    which left the base name colliding across variants in 158 of 1000 seeds:
    two organisations differing by one trailing token are a retrieval confound,
    and a contender that fetched the wrong one would be scored as a
    cross-lingual failure. Reserving a digest class per variant removes the
    collision instead of decorating it.
    """

    for _ in range(256):
        native, latin = wordbank.org_name_cyr(ctx.rng)
        if _name_slot(native) == ctx.variant:
            return native, latin
    raise GenerationError(f"{ctx.template_id}: no organisation name for this variant")


def build(ctx: BuildContext) -> None:
    native_name, latin_alias = _variant_org_name(ctx)
    org = ctx.entity(
        "organization",
        "business",
        aliases=[latin_alias],
        name=native_name,
    )
    archive_count = ctx.rng.randrange(*_ARCHIVE_RANGE)
    routing_count_old = ctx.rng.randrange(*_ROUTING_OLD_RANGE)
    routing_count_new = routing_count_old + ctx.rng.randrange(*_ROUTING_STEP_RANGE)

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
        variants=VARIANTS,
        build=build,
    )
)
