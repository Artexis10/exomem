"""The synthetic rich-episode fixture (close-memory-loop task 1.3).

One ordinary turn about a small bread project carries every kind of durable
change the first episode slice has to route, and two that it must not:

* a new piece of equipment, created under the governed ``equipment`` type the
  world registers (a dynamic type, not a core one);
* a new stable facet of an existing supplier, which hydrates that supplier
  instead of creating a second one;
* a direct label observation and a reported formulation of the supplier's
  product, which share one home, keep their different provenance, and
  connect to the supplier rather than being copied onto the earlier sourcing
  and comparison notes whose scopes do not own them;
* an experiment event, which goes to the existing bake log as an event and
  is never upgraded into a causal conclusion;
* an ambiguous owner ("Corran" is both a person and a kitchen), which is
  written to neither and linked to neither;
* a mentioned possibility, which creates no Planning commitment.

Every candidate's admissible homes, routes, dispositions and provenance are
frozen in :data:`CANDIDATES` before any write, and each names the vault
expectations that enforce it. The actor sees only :func:`actor_view`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import observation as obs
from .contract import (
    EXTENSION,
    Admissible,
    AnyOf,
    AttributedLines,
    BuiltWorld,
    Candidate,
    CaptureCheck,
    CoLocated,
    CoMention,
    EntityCount,
    EntityIntact,
    EntitySeed,
    EntityTypeSeed,
    Expectation,
    FieldIs,
    FixtureError,
    HedgedLines,
    LaterUse,
    Mentions,
    NoNewEdge,
    NoNewMention,
    NoFutureRecords,
    NoNewPlanning,
    NoteSeed,
    PreCapture,
    RecordItem,
    RecordsKept,
    RecordsSeed,
    Select,
    TypedEdge,
    VaultState,
    build_world,
    check_expectations,
    selects_of,
    semantics_fingerprint,
    sha256_json,
    validate_candidates,
)

FIXTURE_ID = "memory-loop-rich-episode-v1"

INVENTED_NAMES: tuple[str, ...] = ("Tessel", "Wrenfold", "Lowmere", "Corran", "Hale")
ORDINARY_CAPITALIZED: tuple[str, ...] = (
    "Anything",
    "April",
    "Baker",
    "Bake",
    "Baking",
    "Busy",
    "Equipment",
    "Grain",
    "I",
    "In",
    "July",
    "Kitchen",
    "Mill",
    "Monday",
    "On",
    "Rye",
    "September",
    "Shared",
    "Stone",
    "Street",
    "The",
    "Thursdays",
    "Tuesday",
    "A",
)

# --------------------------------------------------------------------------- #
# Pre-capture world
# --------------------------------------------------------------------------- #

BAKE_LOG = RecordsSeed(
    key="records_bakes",
    manifest_path="Knowledge Base/Records/Bake Log/_collection.md",
    exomem_id="30000000-0000-4000-8000-000000000001",
    title="Bake log",
    claims=("bake log", "test loaves", "hydration", "crumb"),
    natural_key=("baked_on", "loaf"),
    fields=(
        ("baked_on", "date", True, ()),
        ("loaf", "string", True, ()),
        ("flour", "string", False, ()),
        ("oven", "string", False, ()),
        ("hydration", "string", False, ()),
        ("outcome", "string", False, ()),
        ("note", "string", False, ()),
    ),
    items=(
        (
            "30000000-0000-4000-8000-000000000011",
            (
                ("baked_on", "2026-09-06"),
                ("loaf", "rye sourdough"),
                ("flour", "stoneground rye"),
                ("oven", "home oven"),
                ("hydration", "75%"),
                ("outcome", "open crumb"),
            ),
        ),
    ),
    description="Each bake: what went in, which oven, and how it came out.",
)

WORLD = PreCapture(
    types=(
        EntityTypeSeed(
            type_id="equipment",
            folder="Equipment",
            label="Equipment",
            aliases=("kit item",),
            parent="concept",
            guidance="A physical tool or appliance with its own settings and history.",
        ),
    ),
    entities=(
        EntitySeed(
            key="org_wrenfold",
            entity_type="organization",
            name="Wrenfold Mill",
            summary="Stone mill in the next county.",
        ),
        EntitySeed(
            key="org_lowmere",
            entity_type="organization",
            name="Lowmere Grain",
            summary="Grain merchant.",
        ),
        EntitySeed(
            key="person_corran",
            entity_type="person",
            name="Corran Hale",
            summary="Baker who shares the community kitchen.",
            aliases=("Corran",),
        ),
        EntitySeed(
            key="org_corran",
            entity_type="organization",
            name="Corran Street Kitchen",
            summary="Shared community kitchen.",
            aliases=("Corran",),
        ),
    ),
    notes=(
        NoteSeed(
            key="note_comparison",
            title="Rye supplier comparison",
            slug="rye-supplier-comparison",
            observation=(
                "In spring 2026 Wrenfold Mill beat Lowmere Grain on price and delivery "
                "reliability for rye flour."
            ),
        ),
        NoteSeed(
            key="note_sourcing",
            title="Rye sourcing",
            slug="rye-sourcing",
            observation="Rye flour has come from Wrenfold Mill since April 2026, after the supplier comparison.",
            category="decision",
        ),
    ),
    records=(BAKE_LOG,),
)

# --------------------------------------------------------------------------- #
# Actor input
# --------------------------------------------------------------------------- #

TURNS: tuple[str, ...] = (
    "Busy week on the bread front. The new Tessel deck oven arrived on Monday 22 September: "
    "two stone decks, steam injection, and it tops out at 300 °C. Wrenfold Mill have moved my "
    "rye deliveries to Thursdays. The sack of their stoneground rye that came this week says "
    "milled July 2026 and 12.5% protein on the label, and their miller told me it's a blend "
    "from two harvests, though I haven't seen that written anywhere. On Tuesday 23 September "
    "I baked a test loaf with it at 78% hydration in the Tessel, and the crumb came out denser "
    "than usual; I honestly can't tell yet whether that's the flour or the oven. Corran's "
    "proofing cabinet is still on loan to me, which helps. I might try Lowmere's rye next month.",
)
#: The day the actor's turn was said. A Records row dated after it records
#: intent, not an observed outcome.
TURN_DATE = "2026-09-24"
LATER_TURN = "Baking rye again this weekend. Anything I should bear in mind about the flour and the new oven?"

# --------------------------------------------------------------------------- #
# Evaluator side
# --------------------------------------------------------------------------- #

_OVEN = Select(entity_type="equipment", tokens=("tessel",))
_SUPPLIER = Select(key="org_wrenfold")
_RYE_PAGE = Select(kind="page", any_tokens=("rye", "flour", "stoneground"), created_only=True)
_CORRAN = Select(tokens=("corran",))
_PRODUCT_MARKERS = ("12.5", "two harvests", "blend", "blended", "78%", "denser", "tessel")
#: A focused product page is about its supplier (core ``about_entity``, page
#: to supplier), or relates to it by a governed "supplied by" meaning.
_PRODUCT_TO_SUPPLIER = (
    Admissible(relation="about_entity", direction="forward"),
    Admissible(relation=EXTENSION, direction="either"),
)

EXPECTATIONS: tuple[Expectation, ...] = (
    EntityCount(
        key="rich-episode/oven-identity",
        polarity="positive",
        select=_OVEN,
        exactly=1,
        reason="The new oven is equipment with its own settings, under the registered type.",
    ),
    Mentions(
        key="rich-episode/oven-facets",
        polarity="positive",
        select=_OVEN,
        groups=(("stone",), ("steam",), ("300",)),
        reason="Decks, steam and temperature are the oven's own facets.",
    ),
    Mentions(
        key="rich-episode/supplier-hydrated",
        polarity="positive",
        select=_SUPPLIER,
        groups=(("thursday", "thursdays"),),
        reason="A new delivery day is a stable facet of the existing supplier.",
    ),
    EntityCount(
        key="rich-episode/one-supplier",
        polarity="positive",
        select=Select(entity_type="organization", tokens=("wrenfold",)),
        exactly=1,
        reason="Hydration before duplication: no second supplier.",
    ),
    EntityIntact(
        key="rich-episode/supplier-intact",
        polarity="positive",
        entity="org_wrenfold",
        reason="The supplier keeps its identity.",
    ),
    CoLocated(
        key="rich-episode/rye-facts-one-home",
        polarity="positive",
        groups=(("12.5",), ("july",), ("two harvests", "blend", "blended")),
        homes=(_SUPPLIER, _RYE_PAGE),
        reason=(
            "The label and the reported blend describe one product: they share a home, which is "
            "the supplier or a focused product page, never a related page whose scope does not own them."
        ),
    ),
    AttributedLines(
        key="rich-episode/blend-attributed",
        polarity="positive",
        claim=("two harvests", "blend", "blended"),
        source=("miller", "wrenfold"),
        hedge=(
            "per",
            "likely",
            "said",
            "says",
            "told",
            "according",
            "reported",
            "claims",
            "unverified",
            "unconfirmed",
            "not confirmed",
            "not seen",
            "haven't seen",
            "not written",
            "not documented",
        ),
        reason="The blend is the miller's report, unverified; it never becomes a direct fact.",
    ),
    AnyOf(
        key="rich-episode/product-linked-to-supplier",
        polarity="positive",
        options=(
            Mentions(
                key="rich-episode/product-linked-to-supplier/on-supplier",
                polarity="positive",
                select=_SUPPLIER,
                groups=(("12.5",),),
                reason="The product facts live on the supplier itself.",
            ),
            TypedEdge(
                key="rich-episode/product-linked-to-supplier/edge",
                polarity="positive",
                source=_RYE_PAGE,
                target=_SUPPLIER,
                admissible=_PRODUCT_TO_SUPPLIER,
                reason="A focused product page relates to its supplier by a truthful typed relation.",
            ),
        ),
        reason="The product connects to its existing supplier instead of standing alone.",
    ),
    NoNewMention(
        key="rich-episode/comparison-untouched",
        polarity="negative",
        select=Select(key="note_comparison"),
        markers=_PRODUCT_MARKERS,
        reason="The spring comparison's scope does not own this week's product facts.",
    ),
    NoNewMention(
        key="rich-episode/sourcing-untouched",
        polarity="negative",
        select=Select(key="note_sourcing"),
        markers=_PRODUCT_MARKERS,
        reason="The sourcing decision's scope does not own this week's product facts.",
    ),
    RecordItem(
        key="rich-episode/trial-recorded",
        polarity="positive",
        collection="records_bakes",
        where=(("baked_on", FieldIs(equals=("2026-09-23",))),),
        expect=(
            ("oven", FieldIs(tokens=("tessel",))),
            ("hydration", FieldIs(any_of=("78",))),
            ("flour", FieldIs(any_of=("rye", "wrenfold"))),
            ("outcome", FieldIs(any_of=("dense", "denser"))),
        ),
        reason="The test bake is an event in the existing bake log.",
    ),
    RecordsKept(
        key="rich-episode/bake-history-kept",
        polarity="positive",
        collection="records_bakes",
        reason="Earlier bakes stay as they were.",
    ),
    HedgedLines(
        key="rich-episode/no-causal-claim",
        polarity="negative",
        subject=("dense", "denser", "crumb"),
        causal=("because", "caused", "causes", "due to", "made the", "makes the", "results in", "resulted in", "led to"),
        hedge=(
            "not sure",
            "can't tell",
            "cannot tell",
            "unclear",
            "unknown",
            "whether",
            "may",
            "might",
            "possibly",
            "uncertain",
            "not yet",
            "undetermined",
        ),
        reason="An exposure during a trial is an event, not evidence of what caused the result.",
    ),
    NoNewEdge(
        key="rich-episode/no-causal-edge",
        polarity="negative",
        relations=("causes", "caused_by"),
        reason="No relation turns the trial into a cause.",
    ),
    NoNewMention(
        key="rich-episode/owner-not-on-person",
        polarity="negative",
        select=Select(key="person_corran"),
        markers=("proofing", "cabinet", "loan"),
        reason="'Corran' names a person and a kitchen; the cabinet's owner is unresolved.",
    ),
    NoNewMention(
        key="rich-episode/owner-not-on-kitchen",
        polarity="negative",
        select=Select(key="org_corran"),
        markers=("proofing", "cabinet", "loan"),
        reason="'Corran' names a person and a kitchen; the cabinet's owner is unresolved.",
    ),
    NoNewEdge(
        key="rich-episode/no-edge-to-either-owner",
        polarity="negative",
        target=_CORRAN,
        reason="No relation resolves the ambiguous owner to either identity.",
    ),
    EntityIntact(
        key="rich-episode/person-intact",
        polarity="positive",
        entity="person_corran",
        reason="The shared alias stays where it was.",
    ),
    EntityIntact(
        key="rich-episode/kitchen-intact",
        polarity="positive",
        entity="org_corran",
        reason="The shared alias stays where it was.",
    ),
    NoNewPlanning(
        key="rich-episode/no-planning",
        polarity="negative",
        markers=("lowmere",),
        reason="'I might try' is a possibility, not an expressed intent: no plan holds it; a bake-log line may note it.",
    ),
    NoFutureRecords(
        key="rich-episode/no-future-records",
        polarity="negative",
        turn_date=TURN_DATE,
        reason="Records hold observed outcomes: a row dated after the turn is intent misrouted from Planning.",
    ),
    CoMention(
        key="rich-episode/owner-unresolved-everywhere",
        polarity="negative",
        first=("proofing", "cabinet"),
        second=("corran hale", "corran street kitchen", "corran street"),
        reason="No page, new or old, resolves the cabinet's ambiguous owner to either identity.",
    ),
    EntityCount(
        key="rich-episode/one-lowmere",
        polarity="positive",
        select=Select(entity_type="organization", tokens=("lowmere",)),
        exactly=1,
        reason="A mentioned alternative supplier is not duplicated.",
    ),
)

_RYE_HOMES = ("new:rye-product", "existing:org_wrenfold")
_RYE_ROUTES = ("focused_note", "entity", "existing_page", "semantic_unit")

CANDIDATES: tuple[Candidate, ...] = (
    Candidate(
        key="oven",
        statement="The Tessel deck oven arrived 22 September 2026: two stone decks, steam injection, up to 300 °C.",
        homes=("new:tessel-oven",),
        routes=("entity",),
        dispositions=("routed",),
        provenance="direct",
        checked_by=("rich-episode/oven-identity", "rich-episode/oven-facets"),
    ),
    Candidate(
        key="delivery-day",
        statement="Wrenfold Mill now deliver rye on Thursdays.",
        homes=("existing:org_wrenfold",),
        routes=("existing_page", "semantic_unit"),
        dispositions=("routed",),
        provenance="direct",
        checked_by=(
            "rich-episode/supplier-hydrated",
            "rich-episode/one-supplier",
            "rich-episode/supplier-intact",
        ),
    ),
    Candidate(
        key="rye-label",
        statement="The stoneground rye sack label says milled July 2026, 12.5% protein.",
        homes=_RYE_HOMES,
        routes=_RYE_ROUTES,
        dispositions=("routed",),
        provenance="direct",
        same_home_as=("rye-blend",),
        checked_by=(
            "rich-episode/rye-facts-one-home",
            "rich-episode/comparison-untouched",
            "rich-episode/sourcing-untouched",
        ),
    ),
    Candidate(
        key="rye-blend",
        statement="Wrenfold's miller said the rye blends two harvests; not seen in writing.",
        homes=_RYE_HOMES,
        routes=_RYE_ROUTES,
        dispositions=("routed",),
        provenance="reported",
        attributed_to="the miller at Wrenfold Mill",
        uncertain=True,
        same_home_as=("rye-label",),
        checked_by=("rich-episode/rye-facts-one-home", "rich-episode/blend-attributed"),
    ),
    Candidate(
        key="rye-supplier-link",
        statement="The rye is Wrenfold Mill's product: a relation to the existing supplier, not a copy.",
        homes=_RYE_HOMES,
        routes=("relation_only", "focused_note", "entity", "existing_page", "semantic_unit"),
        dispositions=("routed",),
        provenance="direct",
        checked_by=("rich-episode/product-linked-to-supplier",),
    ),
    Candidate(
        key="trial-bake",
        statement=(
            "23 September 2026: rye test loaf at 78% hydration in the Tessel; denser crumb, cause "
            "(flour or oven) unknown."
        ),
        homes=("existing:records_bakes",),
        routes=("records",),
        dispositions=("routed",),
        provenance="event",
        uncertain=True,
        checked_by=(
            "rich-episode/trial-recorded",
            "rich-episode/bake-history-kept",
            "rich-episode/no-causal-claim",
            "rich-episode/no-causal-edge",
        ),
    ),
    Candidate(
        key="ambiguous-owner",
        statement="The loaned proofing cabinet belongs to 'Corran', which names both a person and a kitchen.",
        homes=("none",),
        routes=("no_capture",),
        dispositions=("uncertain", "deferred", "no_capture"),
        provenance="direct",
        checked_by=(
            "rich-episode/owner-not-on-person",
            "rich-episode/owner-not-on-kitchen",
            "rich-episode/no-edge-to-either-owner",
            "rich-episode/owner-unresolved-everywhere",
            "rich-episode/person-intact",
            "rich-episode/kitchen-intact",
        ),
    ),
    Candidate(
        key="possibility",
        statement="Might try Lowmere's rye next month.",
        homes=("none",),
        routes=("no_capture",),
        dispositions=("no_capture",),
        provenance="direct",
        checked_by=("rich-episode/no-planning", "rich-episode/no-future-records", "rich-episode/one-lowmere"),
    ),
)

LATER_USE = LaterUse(
    useful=(
        "The rye was milled July 2026 at 12.5% protein; the two-harvest blend is only the miller's "
        "word; the 78% test loaf in the Tessel came out denser with the cause not yet known; the "
        "oven has two stone decks and steam."
    ),
    wrong=(
        "The rye is a two-harvest blend.",
        "The flour made the crumb dense.",
        "The proofing cabinet belongs to Corran Hale.",
        "The proofing cabinet belongs to Corran Street Kitchen.",
    ),
)

# --------------------------------------------------------------------------- #
# Frozen pins (re-pinned deliberately; the tests refuse drift)
# --------------------------------------------------------------------------- #

FIXTURE_SHA256 = "3d8c3355efa982bc557ee987699655c1bc7bb01f3c34de603b6e5a9a855143a2"
ACTOR_SHA256 = "b871a3e8f38fb59115bb96744b5392a1348866f4a4d3efb144961d7f20590e45"
EVALUATOR_SHA256 = "51eff448d5690d497863bc15fdc8eed5a5ede9a7ef5b76d54e84feab05b81683"
PRE_CAPTURE_SHA256 = "1b6ecad6bf192e1768b27bf3bd8d85bc1bf179542e8795e2b73fd8b2e79c53aa"

# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #


def actor_view() -> dict[str, Any]:
    return {"fixture_id": FIXTURE_ID, "turns": list(TURNS), "later_turn": LATER_TURN}


def actor_sha256() -> str:
    return sha256_json(actor_view())


def pre_capture_spec_sha256() -> str:
    return sha256_json(WORLD)


def evaluator_sha256() -> str:
    return sha256_json(
        {
            "semantics": semantics_fingerprint(),
            "candidates": CANDIDATES,
            "expectations": EXPECTATIONS,
            "later_use": LATER_USE,
        }
    )


def fixture_sha256() -> str:
    return sha256_json(
        {"fixture": FIXTURE_ID, "actor": actor_view(), "world": WORLD, "evaluator": evaluator_sha256()}
    )


def frozen() -> obs.Frozen:
    """The module's pins, which a run binds to before any effect."""

    return obs.Frozen(
        fixture_id=FIXTURE_ID,
        actor_sha256=ACTOR_SHA256,
        pre_capture_sha256=PRE_CAPTURE_SHA256,
        evaluator_sha256=EVALUATOR_SHA256,
        turns_sha256=obs.turns_sha256(TURNS),
        later_turn_sha256=obs.text_sha256(LATER_TURN),
        shipped_prompt_sha256=obs.shipped_prompt_sha256(),
    )


def void_reasons(record: obs.NoNudgeObservation) -> tuple[str, ...]:
    return obs.void_reasons(record, frozen())


def build_pre_capture(root: Path) -> BuiltWorld:
    return build_world(root, WORLD, world_id=FIXTURE_ID)


def check_capture(world: BuiltWorld, before: VaultState, after: VaultState) -> CaptureCheck:
    return check_expectations(EXPECTATIONS, world=world.key_to_path, before=before, after=after)


def assert_manifest_consistent() -> None:
    keys = [item.key for item in EXPECTATIONS]
    if len(keys) != len(set(keys)):
        raise FixtureError("expectation keys must be unique")
    validate_candidates(CANDIDATES, world_keys=WORLD.keys(), expectation_keys=keys)
    enforced = {check for candidate in CANDIDATES for check in candidate.checked_by}
    if enforced != set(keys):
        raise FixtureError(f"expectations not tied to a candidate: {sorted(set(keys) - enforced)}")
    world_keys = set(WORLD.keys())
    for expectation in EXPECTATIONS:
        for select in selects_of(expectation):
            if select.key is not None and select.key not in world_keys:
                raise FixtureError(f"{expectation.key} selects {select.key}, absent from the world")
        for name in ("entity", "collection"):
            value = getattr(expectation, name, None)
            if value is not None and value not in world_keys:
                raise FixtureError(f"{expectation.key} names {value}, absent from the world")
