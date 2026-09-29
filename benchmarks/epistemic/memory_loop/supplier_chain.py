"""Operator/site and supplier-chain acceptance fixtures (close-memory-loop 1.5).

Six ordinary episodes over one synthetic apple-growing valley. Each starts
from its own frozen pre-capture world, built through the product's writers,
and carries frozen positives and negatives that a capture must leave true:

* ``org-and-site`` -- an organization and the physical site that shares its
  name are distinct identities; site facts land on the site;
* ``multi-role`` -- one organization is producer, operator and supplier,
  with no identity per role, and an incidental trading name makes no brand;
* ``shared-name`` -- a claim naming a surface both identities carry, with no
  context that separates them, is written to neither;
* ``mixed-purchase`` -- a seller's lots keep their own origin: its own label,
  a partner producer reported by the seller, and an unknown origin that stays
  unknown although the partner list is known;
* ``operator-succession`` -- a new operator is a new identity, the site keeps
  its identity, and the operator history keeps the old assignment while the
  current operator is the new one;
* ``brand`` -- an independently useful label several organizations sell
  under is its own identity, linked by a specific registered relation.

The site and brand kinds are governed extension types registered through
``schema_memory`` in every world, never product defaults. Farm words are
acceptance examples; nothing here is a product ontology.

A small, stable API for consumers:

* :data:`EPISODES` / :func:`episode` -- the actor side (turns, later turn);
* :data:`MANIFEST` / :func:`expectations_for` -- frozen positives/negatives;
* :func:`build_pre_capture` -- the episode's world through product writers;
* :func:`check_capture` -- model-free verdicts over before/after readback;
* :data:`SAME_NAME_WORLD` -- a world whose successful build is the writer
  acceptance for a justified same-name distinct identity, committed on the
  seed's explicit ``distinct`` decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import observation as obs
from .contract import (
    EXTENSION,
    Admissible,
    AnyOf,
    AttributedLines,
    BuiltWorld,
    CaptureCheck,
    Distinct,
    EntityCount,
    EntityIntact,
    EntitySeed,
    EntityTypeSeed,
    Expectation,
    FieldIs,
    FixtureError,
    LaterUse,
    LatestRecord,
    Mentions,
    NoEdgeBetween,
    NoNewMention,
    NoteSeed,
    PreCapture,
    RecordItem,
    RecordsKept,
    RecordsSeed,
    RelationSeed,
    RelationTypeSeed,
    Select,
    TypedEdge,
    VaultState,
    build_world,
    check_expectations,
    selects_of,
    semantics_fingerprint,
    sha256_json,
)

FIXTURE_SET_ID = "memory-loop-supplier-chain-v1"

#: Every proper name in this fixture. Invented for it; none names a real
#: person, business, place or project.
INVENTED_NAMES: tuple[str, ...] = ("Merrow", "Pellow", "Callow", "Tarrow", "Crown", "Gold", "Mill")
#: Ordinary capitalized words the turns and seeds use (sentence starts,
#: weekdays, months, apple varieties, English words in titles).
ORDINARY_CAPITALIZED: tuple[str, ...] = (
    "A",
    "And",
    "Apple",
    "Brand",
    "Bramleys",
    "Cider",
    "Company",
    "Cox",
    "Drove",
    "Farm",
    "Finally",
    "Fruit",
    "Grabbed",
    "Had",
    "I",
    "Merrow",
    "Orchard",
    "Orchards",
    "Picked",
    "Purchases",
    "Russets",
    "Saturday",
    "September",
    "Since",
    "Site",
    "Someone",
    "Street",
    "Tell",
    "The",
    "Wednesday",
    "What",
    "Which",
    "Who",
)

#: The facets task 1.5 names, each covered by at least one episode.
FACETS: tuple[str, ...] = (
    "distinct_organization_and_site",
    "one_organization_several_roles",
    "shared_ambiguous_name",
    "independently_useful_brand",
    "incidental_brand",
    "mixed_producer_purchase",
    "supplier_to_producer_provenance",
    "unknown_lot_origin",
    "operator_succession",
)

# --------------------------------------------------------------------------- #
# Pre-capture world seeds
# --------------------------------------------------------------------------- #

TYPES: tuple[EntityTypeSeed, ...] = (
    EntityTypeSeed(
        type_id="site",
        folder="Sites",
        label="Site",
        aliases=("physical site",),
        parent="concept",
        guidance="A physical place with its own identity, apart from whoever runs or owns it.",
    ),
    EntityTypeSeed(
        type_id="brand",
        folder="Brands",
        label="Brand",
        aliases=("trading label",),
        parent="concept",
        guidance="A label or mark whose identity can change apart from any one organization.",
    ),
)

ORG_MERROW = EntitySeed(
    key="org_merrow",
    entity_type="organization",
    name="Merrow Farm",
    summary="Apple business based in Tarrow valley.",
)
#: The site's title differs from the organization's; it also answers to the
#: shared name, which is what makes "Merrow Farm" ambiguous in these worlds.
SITE_MERROW = EntitySeed(
    key="site_merrow",
    entity_type="site",
    name="Merrow Farm Orchard",
    summary="Orchard on the north slope of Tarrow valley.",
    aliases=("Merrow Farm",),
)
ORG_PELLOW = EntitySeed(
    key="org_pellow",
    entity_type="organization",
    name="Pellow Orchards",
    summary="Fruit business in Tarrow valley.",
)
STALL_NOTE = NoteSeed(
    key="note_stall",
    title="Merrow Farm stall",
    slug="merrow-farm-stall",
    observation=(
        "The Merrow Farm market stall also sells fruit bought in from Pellow Orchards "
        "and other valley growers."
    ),
)
PURCHASES = RecordsSeed(
    key="records_purchases",
    manifest_path="Knowledge Base/Records/Fruit Purchases/_collection.md",
    exomem_id="20000000-0000-4000-8000-000000000001",
    title="Fruit purchases",
    claims=("fruit purchases", "lots", "seller", "producer"),
    natural_key=("lot",),
    fields=(
        ("lot", "string", True, ()),
        ("bought_on", "date", True, ()),
        ("seller", "string", True, ()),
        ("producer", "string", False, ()),
        ("origin", "enum", True, ("label", "reported", "unknown")),
        ("origin_source", "string", False, ()),
        ("note", "string", False, ()),
    ),
    items=(
        (
            "20000000-0000-4000-8000-000000000011",
            (
                ("lot", "0907-A"),
                ("bought_on", "2026-09-07"),
                ("seller", "Merrow Farm"),
                ("producer", "Merrow Farm"),
                ("origin", "label"),
            ),
        ),
    ),
    description="Fruit bought by lot: who sold it, who grew it, and how that is known.",
)
OPERATORS = RecordsSeed(
    key="records_operators",
    manifest_path="Knowledge Base/Records/Site Operators/_collection.md",
    exomem_id="20000000-0000-4000-8000-000000000002",
    title="Site operators",
    claims=("site operators", "operator changes", "orchard operator"),
    natural_key=("site", "since"),
    fields=(
        ("site", "string", True, ()),
        ("operator", "string", True, ()),
        ("since", "date", True, ()),
        ("source", "string", False, ()),
    ),
    items=(
        (
            "20000000-0000-4000-8000-000000000021",
            (
                ("site", "Merrow Farm Orchard"),
                ("operator", "Merrow Farm"),
                ("since", "2011-03-01"),
                ("source", "stall board"),
            ),
        ),
    ),
    description="Who runs each site, from when; the newest assignment is the current one.",
)

#: The governed meaning "runs a site day to day", registered in the
#: succession world so the predecessor's edge exists before capture.
OPERATES = RelationTypeSeed(
    relation="vault.operates",
    parent="relates_to",
    description="Runs a site day to day.",
)

#: A same-name site beside its organization. Building this world through the
#: public entity writer is the acceptance that a justified distinct identity
#: stays expressible despite an overlapping surface name.
SAME_NAME_WORLD = PreCapture(
    types=TYPES,
    entities=(
        ORG_MERROW,
        EntitySeed(
            key="site_same_name",
            entity_type="site",
            name="Merrow Farm",
            summary="Orchard on the north slope of Tarrow valley.",
            identity_decision="distinct",
        ),
    ),
)

# --------------------------------------------------------------------------- #
# Episodes (the actor side)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Episode:
    episode_id: str
    world: PreCapture
    #: The ordinary turns of the capture session, verbatim.
    turns: tuple[str, ...]
    #: The one turn of a later fresh session; withheld until that session.
    later_turn: str


EPISODES: tuple[Episode, ...] = (
    Episode(
        episode_id="org-and-site",
        world=PreCapture(types=TYPES, entities=(ORG_MERROW,)),
        turns=(
            "Drove out to Merrow Farm on Saturday to see where the apples actually grow. "
            "The orchard itself is about forty acres on the north slope of Tarrow valley, "
            "twenty minutes past the company's office on Mill Street, and their own-label "
            "boxes all come off that slope.",
        ),
        later_turn="Which slope do the Merrow Farm own-label apples come off?",
    ),
    Episode(
        episode_id="multi-role",
        world=PreCapture(types=TYPES, entities=(ORG_MERROW, SITE_MERROW)),
        turns=(
            "Merrow Farm grow nearly everything on their Saturday stall themselves, they run "
            "the orchard on the north slope day to day, and they're who I buy my apples from "
            "most weeks. The chalkboard over the stall says 'Merrow Gold', but that's only "
            "what they call their honeycrisps.",
        ),
        later_turn="What does Merrow Farm actually do?",
    ),
    Episode(
        episode_id="shared-name",
        world=PreCapture(types=TYPES, entities=(ORG_MERROW, SITE_MERROW)),
        turns=(
            "Grabbed my usual bag of apples at the Saturday market. Someone in the queue "
            "reckoned Merrow Farm is changing hands next spring.",
        ),
        later_turn="Tell me about Merrow Farm.",
    ),
    Episode(
        episode_id="mixed-purchase",
        world=PreCapture(
            types=TYPES,
            entities=(ORG_MERROW, SITE_MERROW, ORG_PELLOW),
            notes=(STALL_NOTE,),
            records=(PURCHASES,),
        ),
        turns=(
            "Picked up three boxes from the Merrow Farm stall this morning, Saturday 21 "
            "September. The Bramleys, lot 0921-A, carry their own orchard's label. The "
            "Russets, lot 0921-B, aren't theirs: the lad on the stall said they came in from "
            "Pellow Orchards. And a mixed box of Cox, lot 0921-C, that nobody there could "
            "name a grower for.",
        ),
        later_turn="Who grew the apples in lots 0921-B and 0921-C?",
    ),
    Episode(
        episode_id="operator-succession",
        world=PreCapture(
            types=TYPES,
            entities=(ORG_MERROW, SITE_MERROW),
            records=(OPERATORS,),
            relation_types=(OPERATES,),
            relations=(RelationSeed(source="org_merrow", relation="vault.operates", target="site_merrow"),),
        ),
        turns=(
            "Had a chat at the Merrow Farm stall today, Wednesday 24 September. Since the "
            "first of September the orchard on the north slope has been run by Callow Cider "
            "Company. Merrow still own the land and keep the Saturday stall; they've just "
            "handed the growing over.",
        ),
        later_turn="Who runs the orchard on the north slope now?",
    ),
    Episode(
        episode_id="brand",
        world=PreCapture(types=TYPES, entities=(ORG_MERROW, ORG_PELLOW)),
        turns=(
            "Finally worked out what the 'Tarrow Crown' sticker on the apples means: it isn't "
            "a farm, it's the valley growers' quality mark. Merrow Farm and Pellow Orchards "
            "both sell under it, and the growers' association that runs it sets the grading "
            "rules and can withdraw the mark from anyone who slips.",
        ),
        later_turn="What is the Tarrow Crown sticker?",
    ),
)

# --------------------------------------------------------------------------- #
# Expectations (the evaluator side; never shown to the actor)
# --------------------------------------------------------------------------- #

_ORG_MERROW = Select(key="org_merrow")
_SITE_MERROW_KEY = Select(key="site_merrow")
#: Every world holds at most one site before capture, so counting all sites
#: is exact: a second site under any name is a duplicate, and a new site is
#: found whatever the agent names it.
_ANY_SITE = Select(entity_type="site")
_ANY_MERROW_ORG = Select(entity_type="organization", tokens=("merrow",))
_MERROW = FieldIs(tokens=("merrow",))
_PELLOW = FieldIs(tokens=("pellow",))
#: Relations that would misstate a seller, a user or a mark as an owner or a
#: component of the thing it relates to.
_CALLOW = Select(entity_type="organization", tokens=("callow",))
#: Operating a site and selling under a mark are reusable meanings the core
#: registry lacks, so the truthful edge is a governed extension in either
#: direction; no core relation says either.
_GOVERNED_MEANING = (Admissible(relation=EXTENSION, direction="either"),)
#: Running a site: the core ``operates`` relation (organization to site) or a
#: governed extension. Core ``operates`` is the registered meaning; the
#: succession world's ``vault.operates`` predates it and stays admissible.
_OPERATES_MEANING = (
    Admissible(relation="operates", direction="forward"),
    Admissible(relation=EXTENSION, direction="either"),
)
_HEARSAY_HEDGES = (
    "reckoned",
    "reckons",
    "reckon",
    "said",
    "says",
    "heard",
    "rumour",
    "rumor",
    "unconfirmed",
    "unverified",
    "hearsay",
    "claimed",
    "claims",
    "according",
    "might",
    "may",
    "possibly",
    "reportedly",
    "apparently",
    "per",
    "likely",
)
_CHANGE_OF_HANDS = (
    "changing hands",
    "change hands",
    "changes hands",
    "changed hands",
    "new owner",
    "next spring",
    "being sold",
    "up for sale",
)

#: Each episode's declared candidates and the markers that identify each in an
#: agent's own disposition text. An abstention passes transport only when its
#: dispositions cover these (see ``observation.Frozen``), so a no-capture of
#: anything else does not stand in for weighing the queue hearsay.
DECLARED_CANDIDATES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "shared-name": (("hearsay", _CHANGE_OF_HANDS),),
}


@dataclass(frozen=True)
class EdgeEvidence:
    """The frozen evidence an expected edge must be authored from."""

    expectation: str
    episode_id: str
    span: str


@dataclass(frozen=True)
class EpisodeExpectations:
    episode_id: str
    covers: tuple[str, ...]
    expectations: tuple[Expectation, ...]
    edge_evidence: tuple[EdgeEvidence, ...]
    later_use: LaterUse


MANIFEST: tuple[EpisodeExpectations, ...] = (
    EpisodeExpectations(
        episode_id="org-and-site",
        covers=("distinct_organization_and_site",),
        expectations=(
            EntityCount(
                key="org-and-site/site-identity",
                polarity="positive",
                select=_ANY_SITE,
                exactly=1,
                reason="The orchard is a physical place with durable use (the own-label lots come off it).",
            ),
            EntityCount(
                key="org-and-site/one-organization",
                polarity="positive",
                select=_ANY_MERROW_ORG,
                exactly=1,
                reason="The existing organization is reused, not duplicated.",
            ),
            EntityIntact(
                key="org-and-site/organization-intact",
                polarity="positive",
                entity="org_merrow",
                reason="The organization is neither retyped as a site nor given the site's names.",
            ),
            Distinct(
                key="org-and-site/distinct-identities",
                polarity="positive",
                first=_ORG_MERROW,
                second=_ANY_SITE,
                reason="The company and its orchard are separate referents.",
            ),
            Mentions(
                key="org-and-site/site-facets",
                polarity="positive",
                select=_ANY_SITE,
                groups=(("north slope",), ("forty acres", "40 acres", "forty-acre", "40-acre")),
                reason="The acreage and slope describe the place.",
            ),
            NoNewMention(
                key="org-and-site/office-not-on-site",
                polarity="negative",
                select=_ANY_SITE,
                markers=("mill street",),
                reason="The office belongs to the company, not the orchard.",
            ),
        ),
        edge_evidence=(),
        later_use=LaterUse(
            useful="The own-label apples grow in the orchard on the north slope of Tarrow valley.",
        ),
    ),
    EpisodeExpectations(
        episode_id="multi-role",
        covers=("one_organization_several_roles", "incidental_brand"),
        expectations=(
            Mentions(
                key="multi-role/producer-role",
                polarity="positive",
                select=_ORG_MERROW,
                groups=(("grow", "grows", "grower", "growers", "producer", "produces"),),
                reason="Producing is a role of the one organization.",
            ),
            Mentions(
                key="multi-role/supplier-role",
                polarity="positive",
                select=_ORG_MERROW,
                groups=(("sell", "sells", "seller", "supplier", "supplies"),),
                reason="Supplying is a role of the one organization.",
            ),
            AnyOf(
                key="multi-role/operator-role",
                polarity="positive",
                options=(
                    Mentions(
                        key="multi-role/operator-role/facet",
                        polarity="positive",
                        select=_ORG_MERROW,
                        groups=(("run", "runs", "operate", "operates", "operator"),),
                        reason="The operator role as a facet of the organization.",
                    ),
                    TypedEdge(
                        key="multi-role/operator-role/edge",
                        polarity="positive",
                        source=_ORG_MERROW,
                        target=_SITE_MERROW_KEY,
                        admissible=_OPERATES_MEANING,
                        reason="The operator role as a governed relation to the site it runs.",
                    ),
                ),
                reason="Operating the orchard is a role of the one organization, as a facet or a relation.",
            ),
            EntityCount(
                key="multi-role/one-organization",
                polarity="positive",
                select=_ANY_MERROW_ORG,
                exactly=1,
                reason="No organization per role.",
            ),
            EntityCount(
                key="multi-role/one-site",
                polarity="positive",
                select=_ANY_SITE,
                exactly=1,
                reason="The orchard is the existing site.",
            ),
            EntityIntact(
                key="multi-role/organization-intact",
                polarity="positive",
                entity="org_merrow",
                reason="Roles are facets, not a retyping or renaming.",
            ),
            EntityIntact(
                key="multi-role/site-intact",
                polarity="positive",
                entity="site_merrow",
                reason="The site keeps its identity.",
            ),
            EntityCount(
                key="multi-role/no-trading-name-entity",
                polarity="negative",
                select=Select(tokens=("gold",)),
                exactly=0,
                reason="A chalkboard name for one variety is an incidental trading name.",
            ),
        ),
        edge_evidence=(),
        later_use=LaterUse(
            useful="The company grows most of what it sells, runs the orchard and sells at the Saturday stall.",
            wrong=("Merrow Gold is a separate brand or business.",),
            expected_status="ambiguous",
        ),
    ),
    EpisodeExpectations(
        episode_id="shared-name",
        covers=("shared_ambiguous_name",),
        expectations=(
            NoNewMention(
                key="shared-name/claim-not-on-organization",
                polarity="negative",
                select=_ORG_MERROW,
                markers=_CHANGE_OF_HANDS,
                reason="Nothing separates the company from its orchard, so neither receives the claim.",
            ),
            NoNewMention(
                key="shared-name/claim-not-on-site",
                polarity="negative",
                select=_SITE_MERROW_KEY,
                markers=_CHANGE_OF_HANDS,
                reason="Nothing separates the company from its orchard, so neither receives the claim.",
            ),
            AttributedLines(
                key="shared-name/hearsay-stays-attributed",
                polarity="negative",
                claim=_CHANGE_OF_HANDS,
                source=("someone", "queue", "market", "heard", "rumour", "rumor", "gossip"),
                hedge=_HEARSAY_HEDGES,
                allow_none=True,
                reason="Queue gossip may go unwritten; written anywhere, it keeps its source and its doubt.",
            ),
            EntityIntact(
                key="shared-name/organization-intact",
                polarity="positive",
                entity="org_merrow",
                reason="No alias moves and no identity changes.",
            ),
            EntityIntact(
                key="shared-name/site-intact",
                polarity="positive",
                entity="site_merrow",
                reason="The shared alias stays where it was.",
            ),
            EntityCount(
                key="shared-name/one-organization",
                polarity="positive",
                select=_ANY_MERROW_ORG,
                exactly=1,
                reason="No identity is created to hold the claim.",
            ),
            EntityCount(
                key="shared-name/one-site",
                polarity="positive",
                select=_ANY_SITE,
                exactly=1,
                reason="No identity is created to hold the claim.",
            ),
        ),
        edge_evidence=(),
        later_use=LaterUse(
            useful="Merrow Farm names both the company and its orchard; the answer covers or asks which.",
            wrong=("Merrow Farm is changing hands next spring.",),
            expected_status="ambiguous",
        ),
    ),
    EpisodeExpectations(
        episode_id="mixed-purchase",
        covers=("mixed_producer_purchase", "supplier_to_producer_provenance", "unknown_lot_origin"),
        expectations=(
            RecordItem(
                key="mixed-purchase/own-label-lot",
                polarity="positive",
                collection="records_purchases",
                where=(("lot", FieldIs(equals=("0921-A",))),),
                expect=(
                    ("seller", _MERROW),
                    ("producer", _MERROW),
                    ("origin", FieldIs(equals=("label",))),
                    ("bought_on", FieldIs(equals=("2026-09-21",))),
                ),
                reason="The label directly establishes the seller's own production.",
            ),
            RecordItem(
                key="mixed-purchase/partner-lot",
                polarity="positive",
                collection="records_purchases",
                where=(("lot", FieldIs(equals=("0921-B",))),),
                expect=(
                    ("seller", _MERROW),
                    ("producer", _PELLOW),
                    ("origin", FieldIs(equals=("reported",))),
                    ("origin_source", FieldIs(any_of=("stall", "seller", "merrow", "staff", "lad"))),
                    ("bought_on", FieldIs(equals=("2026-09-21",))),
                ),
                reason="The seller reported the partner producer; the report keeps its source.",
            ),
            RecordItem(
                key="mixed-purchase/unknown-origin-lot",
                polarity="positive",
                collection="records_purchases",
                where=(("lot", FieldIs(equals=("0921-C",))),),
                expect=(
                    ("seller", _MERROW),
                    ("producer", FieldIs(empty=True)),
                    ("origin", FieldIs(equals=("unknown",))),
                    ("bought_on", FieldIs(equals=("2026-09-21",))),
                ),
                reason="Nobody named the grower; a known partner list does not supply one.",
            ),
            RecordsKept(
                key="mixed-purchase/prior-lots-kept",
                polarity="positive",
                collection="records_purchases",
                reason="Earlier purchases are history, not rewritten.",
            ),
            EntityCount(
                key="mixed-purchase/one-seller",
                polarity="positive",
                select=_ANY_MERROW_ORG,
                exactly=1,
                reason="The seller is resolved, not duplicated.",
            ),
            EntityCount(
                key="mixed-purchase/one-partner",
                polarity="positive",
                select=Select(entity_type="organization", tokens=("pellow",)),
                exactly=1,
                reason="The partner producer is resolved, not duplicated.",
            ),
        ),
        edge_evidence=(),
        later_use=LaterUse(
            useful=(
                "Lot 0921-B came from Pellow Orchards, as the stall reported; nobody could say "
                "who grew lot 0921-C."
            ),
            wrong=(
                "Merrow Farm grew lot 0921-B.",
                "Pellow Orchards grew lot 0921-C.",
                "Merrow Farm grew lot 0921-C.",
            ),
        ),
    ),
    EpisodeExpectations(
        episode_id="operator-succession",
        covers=("operator_succession",),
        expectations=(
            EntityCount(
                key="operator-succession/new-operator-identity",
                polarity="positive",
                select=Select(entity_type="organization", tokens=("callow",)),
                exactly=1,
                reason="The new operator is its own organization.",
            ),
            EntityIntact(
                key="operator-succession/site-intact",
                polarity="positive",
                entity="site_merrow",
                reason="An operator change leaves the site's identity alone.",
            ),
            EntityCount(
                key="operator-succession/one-site",
                polarity="positive",
                select=_ANY_SITE,
                exactly=1,
                reason="No second site appears for the new operator.",
            ),
            EntityIntact(
                key="operator-succession/owner-intact",
                polarity="positive",
                entity="org_merrow",
                reason="The previous operator stays the same organization.",
            ),
            Distinct(
                key="operator-succession/distinct-operators",
                polarity="positive",
                first=_ORG_MERROW,
                second=_CALLOW,
                reason="Old and new operators are different identities.",
            ),
            RecordsKept(
                key="operator-succession/history-kept",
                polarity="positive",
                collection="records_operators",
                reason="The earlier assignment stays as history.",
            ),
            RecordItem(
                key="operator-succession/transition-recorded",
                polarity="positive",
                collection="records_operators",
                where=(("operator", FieldIs(tokens=("callow",))),),
                expect=(("site", _MERROW), ("since", FieldIs(equals=("2026-09-01",)))),
                reason="The new assignment carries its effective date.",
            ),
            LatestRecord(
                key="operator-succession/current-operator",
                polarity="positive",
                collection="records_operators",
                where=(("site", _MERROW),),
                order_field="since",
                expect=(("operator", FieldIs(tokens=("callow",))),),
                reason="The newest assignment is the current operator.",
            ),
            NoEdgeBetween(
                key="operator-succession/no-new-ownership",
                polarity="negative",
                first=_CALLOW,
                second=_SITE_MERROW_KEY,
                forbidden=("owns",),
                reason="Merrow still own the land; running it is not owning it.",
            ),
            NoEdgeBetween(
                key="operator-succession/predecessor-not-current",
                polarity="negative",
                first=_ORG_MERROW,
                second=_SITE_MERROW_KEY,
                tolerated=("owns", "relates_to", "links_to", "mentions"),
                reason=(
                    "Ruling: after the handover no edge presents Merrow Farm as running the site; the "
                    "Records history is the only place the predecessor survives. Ownership and generic "
                    "links stay truthful."
                ),
            ),
            AnyOf(
                key="operator-succession/current-operator-on-site",
                polarity="positive",
                options=(
                    TypedEdge(
                        key="operator-succession/current-operator-on-site/edge",
                        polarity="positive",
                        source=_CALLOW,
                        target=_SITE_MERROW_KEY,
                        admissible=_OPERATES_MEANING,
                        reason="The new operator relates to the site by a governed, non-ownership meaning.",
                    ),
                    Mentions(
                        key="operator-succession/current-operator-on-site/facet",
                        polarity="positive",
                        select=_SITE_MERROW_KEY,
                        groups=(("callow",),),
                        reason="Or the site's current-operator facet names the new operator.",
                    ),
                ),
                reason="The site's projection reflects the supported current operator.",
            ),
        ),
        edge_evidence=(),
        later_use=LaterUse(
            useful="Callow Cider Company has run the orchard since 1 September; Merrow Farm still owns the land.",
            wrong=("Merrow Farm runs the orchard now.",),
        ),
    ),
    EpisodeExpectations(
        episode_id="brand",
        covers=("independently_useful_brand",),
        expectations=(
            EntityCount(
                key="brand/brand-identity",
                polarity="positive",
                select=Select(entity_type="brand", tokens=("tarrow", "crown")),
                exactly=1,
                reason="A mark several growers sell under, run by its own body, is independently useful.",
            ),
            EntityCount(
                key="brand/not-an-organization",
                polarity="negative",
                select=Select(entity_type="organization", tokens=("crown",)),
                exactly=0,
                reason="The mark is not typed as an organization.",
            ),
            EntityIntact(
                key="brand/merrow-intact",
                polarity="positive",
                entity="org_merrow",
                reason="The organizations are neither duplicated nor retyped as the brand.",
            ),
            EntityIntact(
                key="brand/pellow-intact",
                polarity="positive",
                entity="org_pellow",
                reason="The organizations are neither duplicated nor retyped as the brand.",
            ),
            TypedEdge(
                key="brand/merrow-sells-under",
                polarity="positive",
                source=_ORG_MERROW,
                target=Select(entity_type="brand", tokens=("tarrow", "crown")),
                admissible=_GOVERNED_MEANING,
                reason="Merrow Farm sells under the mark: a governed meaning the core registry lacks, never ownership.",
            ),
            TypedEdge(
                key="brand/pellow-sells-under",
                polarity="positive",
                source=Select(key="org_pellow"),
                target=Select(entity_type="brand", tokens=("tarrow", "crown")),
                admissible=_GOVERNED_MEANING,
                reason="Pellow Orchards sells under the mark: a governed meaning the core registry lacks, never ownership.",
            ),
        ),
        edge_evidence=(
            EdgeEvidence(
                expectation="brand/merrow-sells-under",
                episode_id="brand",
                span="Merrow Farm and Pellow Orchards both sell under it",
            ),
            EdgeEvidence(
                expectation="brand/pellow-sells-under",
                episode_id="brand",
                span="Merrow Farm and Pellow Orchards both sell under it",
            ),
        ),
        later_use=LaterUse(
            useful="Tarrow Crown is the valley growers' quality mark that Merrow Farm and Pellow Orchards sell under.",
            wrong=("Tarrow Crown is a farm.",),
        ),
    ),
)

# --------------------------------------------------------------------------- #
# Frozen pins: what a run binds to before any effect. Editing an episode, a
# world, an expectation or the shared predicate semantics moves a digest, and
# the tests refuse the module until these are deliberately re-pinned.
# --------------------------------------------------------------------------- #

FIXTURE_SET_SHA256 = "e95e44bc2062102914b04c5769ccfd5397f7935ffcd5f560970aefb0b2bae946"
ACTOR_SHA256: dict[str, str] = {
    "org-and-site": "135e9bf0556b3ef2d817d6f3477d96770c34352cb54b01b8477b554b5804235e",
    "multi-role": "2c03c998b675d80fd6e3ac1ddc92a75e7cd3b994f2461f0a5b87a2388331f42c",
    "shared-name": "2855858646bbec95e7e1df766863ae1b2b452ed6003aefbce0fa60f1e36dc406",
    "mixed-purchase": "d9e6eb14d61eee77ea21080b624ba19219d2631ba2db3e6f167653bc01ca4129",
    "operator-succession": "82fec4ab317834cb76802ca391bca30ce9304da97376687b1169bccc9294d975",
    "brand": "958f06142f148e58e923996de78a106ef3bbb00da56af85a6c4deffdba76c01c",
}
EVALUATOR_SHA256: dict[str, str] = {
    "org-and-site": "071a332bd898c20c96778d3124df84aa9071fcc20ece22b673db842dc69edab9",
    "multi-role": "5f832589cd17ba8d051c8b892ba06ad3f8edb27c0a9aede0918273d95e336de6",
    "shared-name": "91054c3b976568d8d7d96214c5f0093004eb5dc974d13c5b7fc7bfe3e6b43c80",
    "mixed-purchase": "570cc7d61a7dc7ac0a89008495e698ae7fa1333ec9dec407256e04b2f88c0e8f",
    "operator-succession": "d4b1986f3f12b0a38692c67959b25538b68245301a52a9069ad01497f6b4f9f0",
    "brand": "8e4cf35ce282c7e8bc8cccca7e763df35f58878f8661bc8780aead395d7c2e02",
}
PRE_CAPTURE_SHA256: dict[str, str] = {
    "org-and-site": "47a43d1f62175c6696516cfe4c3e822ec2e424d1180dc42dcd81dc4137449f55",
    "multi-role": "44bfb580f7a1a240c997733e2157b1d6471defcec2026d820e17c71f4922eec1",
    "shared-name": "3f75e7b405d8a2949c96c9273c44965611e82c5d4fcc7e750f44d292c47a95f9",
    "mixed-purchase": "bec5a11f232f269fa19c5ac390dbd4fb87b83b5a7e906ea245fc9b4400b44bb2",
    "operator-succession": "61e800f3540a1b094cbaab9d28014b9901ad7ea923af6a88def26a37a6087a57",
    "brand": "6351c8385040c7edca049d7bb7a77513ec5a30a7071d043186dd3804beecfe23",
}

# --------------------------------------------------------------------------- #
# The API
# --------------------------------------------------------------------------- #


def episode(episode_id: str) -> Episode:
    for item in EPISODES:
        if item.episode_id == episode_id:
            return item
    raise FixtureError(f"unknown episode: {episode_id}")


def expectations_for(episode_id: str) -> EpisodeExpectations:
    for item in MANIFEST:
        if item.episode_id == episode_id:
            return item
    raise FixtureError(f"unknown episode: {episode_id}")


def actor_view(episode_id: str) -> dict[str, Any]:
    """Everything the actor may see: its turns, and later the fresh-session turn."""

    item = episode(episode_id)
    return {"episode_id": item.episode_id, "turns": list(item.turns), "later_turn": item.later_turn}


def actor_sha256(episode_id: str) -> str:
    return sha256_json(actor_view(episode_id))


def pre_capture_spec_sha256(episode_id: str) -> str:
    return sha256_json(episode(episode_id).world)


def evaluator_sha256(episode_id: str | None = None) -> str:
    evaluator = MANIFEST if episode_id is None else expectations_for(episode_id)
    candidates = DECLARED_CANDIDATES if episode_id is None else DECLARED_CANDIDATES.get(episode_id, ())
    return sha256_json({"semantics": semantics_fingerprint(), "evaluator": evaluator, "candidates": candidates})


def fixture_set_sha256() -> str:
    return sha256_json(
        {
            "fixture_set": FIXTURE_SET_ID,
            "actor": [actor_view(item.episode_id) for item in EPISODES],
            "worlds": [item.world for item in EPISODES],
            "evaluator": evaluator_sha256(),
        }
    )


def frozen(episode_id: str) -> obs.Frozen:
    """The pins a run of one episode binds to: the module's frozen digests,
    never digests recomputed from the current source."""

    item = episode(episode_id)
    return obs.Frozen(
        fixture_id=f"{FIXTURE_SET_ID}/{episode_id}",
        actor_sha256=ACTOR_SHA256[episode_id],
        pre_capture_sha256=PRE_CAPTURE_SHA256[episode_id],
        evaluator_sha256=EVALUATOR_SHA256[episode_id],
        turns_sha256=obs.turns_sha256(item.turns),
        later_turn_sha256=obs.text_sha256(item.later_turn),
        shipped_prompts=obs.shipped_prompts(),
        candidates=DECLARED_CANDIDATES.get(episode_id, ()),
    )


def void_reasons(record: obs.NoNudgeObservation, episode_id: str) -> tuple[str, ...]:
    return obs.void_reasons(record, frozen(episode_id))


def build_pre_capture(root: Path, episode_id: str) -> BuiltWorld:
    """Build the episode's frozen pre-capture world through product writers."""

    return build_world(root, episode(episode_id).world, world_id=f"{FIXTURE_SET_ID}/{episode_id}")


def check_capture(
    episode_id: str, world: BuiltWorld, before: VaultState, after: VaultState
) -> CaptureCheck:
    """Model-free verdicts for every frozen positive and negative of an episode."""

    return check_expectations(
        expectations_for(episode_id).expectations,
        world=world.key_to_path,
        before=before,
        after=after,
    )


def assert_manifest_consistent() -> None:
    ids = [item.episode_id for item in EPISODES]
    if ids != [item.episode_id for item in MANIFEST] or len(set(ids)) != len(ids):
        raise FixtureError("episodes and expectations must pair one to one, in order")
    covered = {facet for item in MANIFEST for facet in item.covers}
    if covered != set(FACETS):
        raise FixtureError(f"facets not covered exactly: {sorted(set(FACETS) ^ covered)}")
    keys = [expectation.key for item in MANIFEST for expectation in item.expectations]
    if len(keys) != len(set(keys)):
        raise FixtureError("expectation keys must be unique")
    for item in MANIFEST:
        world_keys = set(episode(item.episode_id).world.keys())
        for expectation in item.expectations:
            if not expectation.key.startswith(f"{item.episode_id}/"):
                raise FixtureError(f"{expectation.key} is not scoped to {item.episode_id}")
            for name in ("entity", "collection"):
                value = getattr(expectation, name, None)
                if value is not None and value not in world_keys:
                    raise FixtureError(f"{expectation.key} names {value}, absent from its world")
            for select in selects_of(expectation):
                if select.key is not None and select.key not in world_keys:
                    raise FixtureError(f"{expectation.key} selects {select.key}, absent from its world")
        edge_keys = {evidence.expectation for evidence in item.edge_evidence}
        positive_edges = {
            expectation.key
            for expectation in item.expectations
            if isinstance(expectation, TypedEdge) and expectation.polarity == "positive"
        }
        if edge_keys != positive_edges:
            raise FixtureError(f"{item.episode_id}: every expected edge needs frozen evidence")
        turns = " ".join(episode(item.episode_id).turns)
        for evidence in item.edge_evidence:
            if evidence.span not in turns:
                raise FixtureError(f"{evidence.expectation}: evidence span is not in the episode input")
