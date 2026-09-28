"""Generic synthetic decomposition/replay acceptance (close-memory-loop task 1.7).

One ordinary turn, said while a preferences page is open, carries five
independently reusable objects that differ by retrieval question, subject,
temporal episode and epistemic role:

* a preference with product observations -- the open page's own cluster;
* repeated personal sensory observations this week, with their conditions;
* a historical recovery episode from two years earlier, which keeps its own
  time and is never restated as a current event;
* a baseline ability, the normal against which the observations compare;
* a family member's interpretation, which keeps its speaker and its
  uncertainty and is never omitted or upgraded into a fact.

The acceptance has two sides. On the observation record, decomposition must
come first: every candidate the agent routes has a partition with all four
axes recorded before the first destination is chosen, and objects the
fixture separates stay in separate partitions (:func:`partition_verdict`).
In the vault the capture leaves, the open page keeps only its own cluster,
this week's observations and their interpretation form one new home, the
older episode and the baseline land on new pages (together with this week's
home or apart; that grouping is the agent's), no detail fragments into its
own page, and the attribution and the old episode's time survive.

Declared limit: ``decomposition/preference-not-reversed`` is a phrase
blacklist. It fails only the reversals it lists ("over the Lowfield",
"settled on the Brightwater" and their short forms); a reversal worded any
other way is not caught by it, and no model-free predicate here judges the
direction of a choice in general.

Content is deliberately ordinary perception about tea: no clinical words,
no private names, no source paths. The capture failure that motivated this
fixture is evidence of the need only; none of its content is here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import observation as obs
from .contract import (
    AnyOf,
    AttributedLines,
    BuiltWorld,
    Candidate,
    CaptureCheck,
    CoLocated,
    EntityIntact,
    Expectation,
    FixtureError,
    LaterUse,
    LinesCarry,
    Mentions,
    NewPages,
    NoMixedLines,
    NoNewMention,
    NoteSeed,
    Partition,
    PreCapture,
    Select,
    VaultState,
    build_world,
    check_expectations,
    selects_of,
    semantics_fingerprint,
    sha256_json,
    validate_candidates,
)

FIXTURE_ID = "memory-loop-decomposition-v1"
EXACT_PRIVATE_REPLAY = "local only; binds its own original input and snapshot; never scored here"

INVENTED_NAMES: tuple[str, ...] = ("Lowfield", "Brightwater", "Fenn")
ORDINARY_CAPITALIZED: tuple[str, ...] = (
    "I",
    "It",
    "Monday",
    "My",
    "Normally",
    "September",
    "Shortlisted",
    "Tea",
    "This",
    "Thursday",
    "Wednesday",
    "What",
)

WORLD = PreCapture(
    notes=(
        NoteSeed(
            key="note_tea_prefs",
            title="Tea preferences",
            slug="tea-preferences",
            observation="Shortlisted oolongs: the Lowfield oolong and the Brightwater house blend; still choosing.",
            category="preference",
        ),
    ),
)
#: The page the user has open while speaking. It is one inspected
#: alternative, never a preferred default for everything said.
CURRENTLY_OPEN = "note_tea_prefs"

TURNS: tuple[str, ...] = (
    "I've got my tea preferences page open, so a quick rundown while I think about it. I've "
    "settled on the Lowfield oolong over the Brightwater house blend: the Lowfield tin keeps it "
    "fresher and the second steep holds up. This week, the week of 21 September, my tea tasted "
    "flat to me three mornings running, on Monday, Wednesday and Thursday, each time the first "
    "cup after the kettle had sat overnight. It reminds me of spring 2024, when everything "
    "tasted flat for about a month after I moved flats, and it came back once I started using "
    "the filter jug. Normally I can tell a first steep from a second steep blind about nine "
    "times out of ten. My brother Fenn thinks it's limescale in the new kettle, but he hasn't "
    "actually looked at it.",
)
LATER_TURN = "My tea tasted flat again this morning. What do I know that might help?"

# --------------------------------------------------------------------------- #
# Evaluator side
# --------------------------------------------------------------------------- #

PARTITIONS: dict[str, Partition] = {
    "preference": Partition(
        retrieval_question="Which tea do I prefer, and why?",
        subject="tea products",
        temporal_episode="settled in September 2026",
        epistemic_role="stated preference with direct product observations",
    ),
    "flat-week": Partition(
        retrieval_question="When has my tea tasted flat recently, and in what circumstances?",
        subject="my taste perception",
        temporal_episode="week of 21 September 2026",
        epistemic_role="repeated direct sensory observation",
    ),
    "spring-2024": Partition(
        retrieval_question="What happened when tea tasted flat in spring 2024, and what brought it back?",
        subject="my taste perception",
        temporal_episode="spring 2024",
        epistemic_role="historical recovery episode",
    ),
    "baseline": Partition(
        retrieval_question="How reliably can I normally tell steeps apart?",
        subject="my tasting ability",
        temporal_episode="ongoing",
        epistemic_role="baseline ability",
    ),
    "fenn-view": Partition(
        retrieval_question="What explanations have others offered for the flat taste?",
        subject="explanations of the flat taste",
        temporal_episode="week of 21 September 2026",
        epistemic_role="attributed interpretation, unverified",
    ),
}

#: Markers that identify each expected candidate in an agent's own candidate
#: text (model-free; each is specific to one candidate).
IDENTIFIERS: dict[str, tuple[str, ...]] = {
    "preference": ("settled", "over the brightwater", "house blend"),
    "product-observations": ("tin", "second steep holds"),
    "flat-mornings": ("monday", "wednesday", "overnight", "three mornings"),
    "spring-2024": ("2024", "filter jug"),
    "baseline": ("nine times", "9 times", "nine out of ten", "9 out of 10", "blind"),
    "fenn-view": ("limescale", "fenn"),
}

_OPEN = Select(key="note_tea_prefs")
_CREATED = Select(kind="page", created_only=True)
_ANY_CREATED_ENTITY = Select(entity_type=None, tokens=("lowfield",), created_only=True)

#: The three perception clusters. No new statement fuses two of them.
_THIS_WEEK = ("monday", "wednesday", "thursday", "overnight", "three mornings")
_SPRING_2024 = ("2024", "filter jug", "moved flats")
_BASELINE = ("nine times", "9 times", "nine out of ten", "9 out of 10", "90%", "steep blind")

EXPECTATIONS: tuple[Expectation, ...] = (
    LinesCarry(
        key="decomposition/preference-on-its-page",
        polarity="positive",
        select=_OPEN,
        claim=(
            "preference",
            "settled",
            "chose",
            "chosen",
            "picked",
            "decided",
            "prefer",
            "prefers",
            "preferred",
            "favour",
            "favourite",
            "favor",
            "favorite",
            "over the brightwater",
        ),
        groups=(("lowfield",),),
        reason="Every new statement of the choice on the open page names the tea chosen: Lowfield.",
    ),
    NoNewMention(
        key="decomposition/preference-not-reversed",
        polarity="negative",
        select=_OPEN,
        markers=(
            "over the lowfield",
            "over lowfield",
            "settled on the brightwater",
            "settled on brightwater",
            "prefer the brightwater",
            "prefer brightwater",
        ),
        reason="The capture keeps the direction of the choice.",
    ),
    AnyOf(
        key="decomposition/product-observations-in-scope",
        polarity="positive",
        options=(
            Mentions(
                key="decomposition/product-observations-in-scope/on-page",
                polarity="positive",
                select=_OPEN,
                groups=(("tin",), ("second steep", "second infusion")),
                new_lines=True,
                reason="Product observations sit with the preference they support.",
            ),
            Mentions(
                key="decomposition/product-observations-in-scope/on-product",
                polarity="positive",
                select=_ANY_CREATED_ENTITY,
                groups=(("tin",), ("second steep", "second infusion")),
                new_lines=True,
                reason="Or as facets of the product, if the agent promotes it to an entity.",
            ),
        ),
        reason="The tin and the second steep are in-scope product details, not a page of their own.",
    ),
    NoMixedLines(
        key="decomposition/no-mixed-clusters-in-a-line",
        polarity="negative",
        groups=(_THIS_WEEK, _SPRING_2024, _BASELINE),
        reason="This week's observations, the 2024 episode and the baseline are never fused into one statement.",
    ),
    NoNewMention(
        key="decomposition/open-page-holds-only-its-cluster",
        polarity="negative",
        select=_OPEN,
        ignore_links=True,
        markers=(
            "flat",
            "2024",
            "filter jug",
            "nine times",
            "9 times",
            "nine out of ten",
            "9 out of 10",
            "limescale",
            "fenn",
            "monday",
            "wednesday",
            "overnight",
        ),
        reason="Being open does not make the preferences page the home for perception, history or opinion.",
    ),
    CoLocated(
        key="decomposition/this-week-has-one-home",
        polarity="positive",
        groups=(("monday",), ("wednesday",), ("thursday",), ("overnight",)),
        homes=(_CREATED,),
        reason="This week's repeated observations and their condition form one new cluster home.",
    ),
    CoLocated(
        key="decomposition/interpretation-beside-the-observations",
        polarity="positive",
        groups=(("limescale",), ("wednesday",)),
        homes=(_CREATED,),
        reason="The interpretation of this week's observations is a unit in their home, not its own page.",
    ),
    AttributedLines(
        key="decomposition/interpretation-attributed",
        polarity="positive",
        claim=("limescale",),
        source=("fenn", "brother"),
        hedge=(
            "thinks",
            "thought",
            "suggests",
            "suspects",
            "believes",
            "guess",
            "according",
            "says",
            "said",
            "unverified",
            "unconfirmed",
            "hasn't",
            "has not",
            "not checked",
            "not looked",
            "untested",
            "possible",
            "possibly",
            "might",
            "may",
        ),
        reason="Fenn's interpretation keeps its speaker and its uncertainty wherever it is written.",
    ),
    LinesCarry(
        key="decomposition/old-episode-keeps-its-time",
        polarity="positive",
        claim=("filter jug",),
        groups=(("2024",),),
        reason="The recovery episode is captured with its own time, never as a current event.",
    ),
    CoLocated(
        key="decomposition/old-episode-has-a-home",
        polarity="positive",
        groups=(("2024",), ("filter jug",)),
        homes=(_CREATED,),
        reason="The historical episode lands on a new page (its own or this week's cluster home).",
    ),
    CoLocated(
        key="decomposition/baseline-has-a-home",
        polarity="positive",
        groups=(("nine times", "9 times", "nine out of ten", "9 out of 10", "90%"),),
        homes=(_CREATED,),
        reason="The baseline ability lands on a new page (its own or this week's cluster home).",
    ),
    AnyOf(
        key="decomposition/no-detail-fragmentation",
        polarity="positive",
        options=tuple(
            NewPages(
                key=f"decomposition/no-detail-fragmentation/{count}",
                polarity="positive",
                exactly=count,
                exclude_types=("entity",),
                reason="One to three new cluster homes.",
            )
            for count in (1, 2, 3)
        ),
        reason=(
            "At most one new page per meaningful cluster (this week, the old episode, the baseline); "
            "no page per morning, per product detail or per opinion."
        ),
    ),
    EntityIntact(
        key="decomposition/open-page-intact",
        polarity="positive",
        entity="note_tea_prefs",
        reason="The preferences page stays the same page.",
    ),
)

_PERCEPTION_HOMES = ("new:flat-week", "new:spring-2024", "new:baseline")

CANDIDATES: tuple[Candidate, ...] = (
    Candidate(
        key="preference",
        statement="Settled on the Lowfield oolong over the Brightwater house blend.",
        homes=("existing:note_tea_prefs",),
        routes=("existing_page", "semantic_unit"),
        dispositions=("routed",),
        provenance="preference",
        partition=PARTITIONS["preference"],
        checked_by=(
            "decomposition/preference-on-its-page",
            "decomposition/preference-not-reversed",
            "decomposition/open-page-intact",
            "decomposition/open-page-holds-only-its-cluster",
        ),
    ),
    Candidate(
        key="product-observations",
        statement="The Lowfield tin keeps it fresher; its second steep holds up.",
        homes=("existing:note_tea_prefs", "new:lowfield-product"),
        routes=("existing_page", "semantic_unit", "entity"),
        dispositions=("routed",),
        provenance="direct",
        partition=PARTITIONS["preference"],
        checked_by=("decomposition/product-observations-in-scope", "decomposition/no-detail-fragmentation"),
    ),
    Candidate(
        key="flat-mornings",
        statement=(
            "Tea tasted flat on Monday, Wednesday and Thursday of the week of 21 September 2026, "
            "each the first cup after the kettle sat overnight."
        ),
        homes=("new:flat-week",),
        routes=("focused_note",),
        dispositions=("routed",),
        provenance="direct",
        partition=PARTITIONS["flat-week"],
        checked_by=(
            "decomposition/this-week-has-one-home",
            "decomposition/no-detail-fragmentation",
            "decomposition/no-mixed-clusters-in-a-line",
        ),
    ),
    Candidate(
        key="spring-2024",
        statement=(
            "In spring 2024, after moving flats, tea tasted flat for about a month and came back "
            "once a filter jug was used."
        ),
        homes=_PERCEPTION_HOMES,
        routes=("focused_note", "semantic_unit", "existing_page"),
        dispositions=("routed",),
        provenance="direct",
        partition=PARTITIONS["spring-2024"],
        checked_by=("decomposition/old-episode-keeps-its-time", "decomposition/old-episode-has-a-home"),
    ),
    Candidate(
        key="baseline",
        statement="Normally tells a first steep from a second steep blind about nine times out of ten.",
        homes=_PERCEPTION_HOMES,
        routes=("focused_note", "semantic_unit", "existing_page"),
        dispositions=("routed",),
        provenance="baseline",
        partition=PARTITIONS["baseline"],
        checked_by=("decomposition/baseline-has-a-home",),
    ),
    Candidate(
        key="fenn-view",
        statement="Fenn (the user's brother) thinks the flat taste is limescale in the new kettle; he has not checked.",
        homes=("new:flat-week",),
        routes=("focused_note", "semantic_unit", "existing_page"),
        dispositions=("routed",),
        provenance="attributed",
        attributed_to="Fenn, the user's brother",
        uncertain=True,
        same_home_as=("flat-mornings",),
        partition=PARTITIONS["fenn-view"],
        checked_by=(
            "decomposition/interpretation-beside-the-observations",
            "decomposition/interpretation-attributed",
        ),
    ),
)

LATER_USE = LaterUse(
    useful=(
        "This week the first cup after an overnight kettle tasted flat on three mornings; in spring "
        "2024 a month of flat taste cleared once a filter jug was used; normally you tell steeps "
        "apart nine times in ten; Fenn suspects limescale in the new kettle but has not checked."
    ),
    wrong=(
        "Limescale is causing the flat taste.",
        "The flat taste has lasted since spring 2024.",
    ),
)

# --------------------------------------------------------------------------- #
# Frozen pins (re-pinned deliberately; the tests refuse drift)
# --------------------------------------------------------------------------- #

FIXTURE_SHA256 = "89cb9e404e0467be67da256ce7153a26eae9a5ba0db59592aa35893fdd5f9638"
ACTOR_SHA256 = "5d231888bf6d0dea5fcc4fc91f6522e1147d52540f0e4040394716a49da9ab53"
EVALUATOR_SHA256 = "8546f6a4b92c718851e9162ad420f8296003900b50fd70c3d3c73c47ab952b4c"
PRE_CAPTURE_SHA256 = "49186c3596cc46383672e7e2d507b455e61c8da9f9dbcfd750b577d0d8dfd0ce"

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
            "partitions": PARTITIONS,
            "identifiers": IDENTIFIERS,
            "candidates": CANDIDATES,
            "expectations": EXPECTATIONS,
            "later_use": LATER_USE,
            "currently_open": CURRENTLY_OPEN,
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
        shipped_prompts=obs.shipped_prompts(),
        candidates=tuple(IDENTIFIERS.items()),
    )


def void_reasons(record: obs.NoNudgeObservation) -> tuple[str, ...]:
    return obs.void_reasons(record, frozen())


def build_pre_capture(root: Path) -> BuiltWorld:
    return build_world(root, WORLD, world_id=FIXTURE_ID)


def check_capture(world: BuiltWorld, before: VaultState, after: VaultState) -> CaptureCheck:
    return check_expectations(EXPECTATIONS, world=world.key_to_path, before=before, after=after)


def _hits(text: str, markers: tuple[str, ...]) -> int:
    folded = text.casefold()
    return sum(
        1 for marker in markers if re.search(r"(?<![a-z0-9])" + re.escape(marker) + r"(?![a-z0-9])", folded)
    )


def identify(text: str) -> str | None:
    """The one expected candidate an agent's own candidate text is about.

    The candidate with the most distinct identifier hits wins; a tie names
    none, so a text that merely mentions another object is not counted as it.
    """

    scores = {key: _hits(text, markers) for key, markers in IDENTIFIERS.items()}
    best = max(scores.values())
    winners = [key for key, score in scores.items() if score == best]
    return winners[0] if best > 0 and len(winners) == 1 else None


def _partition_of(candidate_key: str) -> str:
    candidate = next(item for item in CANDIDATES if item.key == candidate_key)
    return next(name for name, partition in PARTITIONS.items() if partition == candidate.partition)


def partition_verdict(record: obs.NoNudgeObservation) -> obs.Verdict:
    """Decomposition before destination, read from the observation record.

    * every expected partition is considered by at least one decomposition
      decision (an abstention still counts as considered);
    * every candidate that receives a destination was decomposed before the
      episode's first destination decision;
    * candidates the fixture puts in different partitions sit in different
      recorded partitions.

    A record with no decomposition decision at all is ``unmeasured``: the
    carrier did not record a partition, which is not evidence of one.
    """

    decompositions = [item for item in record.agent_decisions if item.phase == "decomposition"]
    if not decompositions:
        return obs.Verdict("unmeasured", ("no decomposition decision was recorded",))
    reasons: list[str] = []
    texts: dict[str, str] = {}
    for item in record.agent_decisions:
        if item.candidate_key:
            texts[item.candidate_key] = f"{texts.get(item.candidate_key, '')} {item.text} {item.title or ''}"
    expected_of = {key: identify(text) for key, text in texts.items()}
    partition_ids = {item.candidate_key: item.partition.partition_id for item in decompositions if item.partition}
    decomposed_at = {item.candidate_key: item.seq for item in decompositions}
    considered = {
        _partition_of(expected)
        for item in decompositions
        if (expected := expected_of.get(item.candidate_key)) is not None
    }
    missing = sorted(set(PARTITIONS) - considered)
    if missing:
        reasons.append(f"partitions never considered: {missing}")
    destinations = [item for item in record.agent_decisions if item.phase == "destination"]
    if destinations:
        first = min(item.seq for item in destinations)
        for item in destinations:
            seq = decomposed_at.get(item.candidate_key)
            if seq is None or seq > first:
                reasons.append(f"{item.candidate_key} was not decomposed before the first destination")
    owners: dict[str, set[str]] = {}
    for key, expected in expected_of.items():
        if key in partition_ids and expected is not None:
            owners.setdefault(partition_ids[key], set()).add(_partition_of(expected))
    for partition_id, names in sorted(owners.items()):
        if len(names) > 1:
            reasons.append(f"recorded partition {partition_id} merges {sorted(names)}")
    return obs.Verdict("fail", tuple(reasons)) if reasons else obs.Verdict("pass")


def assert_manifest_consistent() -> None:
    keys = [item.key for item in EXPECTATIONS]
    if len(keys) != len(set(keys)):
        raise FixtureError("expectation keys must be unique")
    validate_candidates(CANDIDATES, world_keys=WORLD.keys(), expectation_keys=keys)
    enforced = {check for candidate in CANDIDATES for check in candidate.checked_by}
    if enforced != set(keys):
        raise FixtureError(f"expectations not tied to a candidate: {sorted(set(keys) ^ enforced)}")
    if set(IDENTIFIERS) != {candidate.key for candidate in CANDIDATES}:
        raise FixtureError("every candidate needs identifiers, and only candidates have them")
    for candidate in CANDIDATES:
        if candidate.partition not in PARTITIONS.values():
            raise FixtureError(f"{candidate.key} has no declared partition")
    for name, partition in PARTITIONS.items():
        for other, second in PARTITIONS.items():
            if name < other and partition == second:
                raise FixtureError(f"{name} and {other} are not distinct partitions")
    for key, markers in IDENTIFIERS.items():
        for other, second in IDENTIFIERS.items():
            if key != other and set(markers) & set(second):
                raise FixtureError(f"{key} and {other} share an identifier")
    world_keys = set(WORLD.keys())
    for expectation in EXPECTATIONS:
        for select in selects_of(expectation):
            if select.key is not None and select.key not in world_keys:
                raise FixtureError(f"{expectation.key} selects {select.key}, absent from the world")
