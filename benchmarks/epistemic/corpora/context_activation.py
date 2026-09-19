"""Fixtures for the context-activation benchmark (OpenSpec change
``add-context-activation-benchmark``): nine cold-start cases (C1-C9) and nine
frequency-matched negative twins (T1-T9), authored before any retrieval is
run, plus the seeded synthetic corpus that mirrors their shapes with
generated names only.

Gold and poison lists are LOGICAL KEYS, never vault paths: :func:`build_corpus`
renders the synthetic corpus and returns the key -> path mapping (mirroring
``scripts/referent_resolution_benchmark.py``'s ``id_to_path`` pattern), and the
same keys are meant to be remapped locally (never committed) to real vault
paths for the private instrument. This is what keeps the committed fixture
manifest generic under the scaffold no-leak rule while still letting the
deterministic scorer (``membench.utility.context_activation``) resolve a
packet's anchor refs against gold/poison for either corpus.

Every twin is a negative control: none carries legitimate gold for its own
paired case's content. A minority (T3, T4, T5, T7, T8) carry a *narrow*
legitimate gold of their own -- a different, unremarkable thing that should
resolve, or stay ambiguous, normally -- so the fixture asserts the absence of
one specific spurious signal (cross-case leakage, false ambiguity, a
fabricated unavailability label, a false supersession mark) rather than mere
silence. T4's narrow gold is the two same-kind person candidates it is
genuinely ambiguous between (a shared first name): the case is winnable
because the twin rule scores `resolved` anchors *outside* that gold, and a
correct packet's own two candidates are never outside it. T3 ("the other
workstream's own item"), T7 ("a scoped variant that resolves") and T8 ("an
unchained active note") follow the same shape. Deviation, flagged for the
pre-registration owner: the spec's literal scenario text ("any twin turn
yields an anchor with status resolved -> the case fails") is read here as
"any ref *outside a twin's own gold* is mentioned anywhere in the packet" --
the strictly literal, resolved-anchor-only reading both misses false
activation injected through units/pointers alone and would make T3/T4/T7/T8's
own explicit design narrative unsatisfiable by construction. See
``membench.utility.context_activation.score_case``.

C9/T9 are the one pair whose corpus tree is part of the fixture's own
identity (spec: "Every fixture and every packet SHALL record the corpus
tree ... it belongs to"). C9 is the grill case's own turn (C2) scored
against the padded (``distractor_count=200``) tree; T9 is C2's *twin's* own
turn (T2, verbatim), scored on that *same* padded tree, gold empty, expected
``unresolved`` -- an ordinary ninth negative twin, proving distractor
padding alone doesn't manufacture a false activation for an unrelated query.
Padding robustness itself (``membench.utility.context_activation.
score_padding_robustness``) compares C9's padded-tree score against C2's own
score on the unpadded tree, never against T9: a twin with a different turn
cannot show what padding did to the *grill query's own* precision or
recall; only the same query scored on two different corpus states can, and
that is what C9-vs-C2 is for. A round-one design (T9 sharing C9's own turn,
scored unpadded) is superseded: it worked for the padding comparison but
cost the ninth negative twin, which this shape restores.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

FIXTURE_SET_ID = "context-activation-fixtures-v1"
CORPUS_ID = "context-activation-corpus-v3"

CASE_IDS: tuple[str, ...] = tuple(f"C{i}" for i in range(1, 10))
TWIN_IDS: tuple[str, ...] = tuple(f"T{i}" for i in range(1, 10))

EXPECTED_STATUSES: tuple[str, ...] = ("resolved", "partial", "ambiguous", "unresolved")

#: Default distractor-page count for C9's padded neighbourhood (design.md D3).
DEFAULT_DISTRACTOR_COUNT = 200
#: The unpadded tree's distractor count -- C2's own corpus state for the
#: padding-robustness comparison against C9 (N1 round-two revision).
BASE_DISTRACTOR_COUNT = 0


class FixtureError(ValueError):
    """Raised for a malformed or inconsistent fixture manifest."""


@dataclass(frozen=True)
class FixtureCase:
    """One authored case or twin. Immutable: editing a field is a new digest."""

    case_id: str
    pairs_with: str | None
    turn: str
    reminder_turn: str
    gold: tuple[str, ...]
    poison: tuple[str, ...]
    roles: tuple[str, ...]
    must_include: tuple[str, ...]
    must_exclude: tuple[str, ...]
    expected_status: str
    #: A5's hand-authored, per-case oracle-packet prose (task 3, B6).
    #: Deliberately *not* derived from ``must_include``: A5 is meant to be an
    #: independent ceiling estimate, and deriving its content from the same
    #: facts the reminder-turn test checks would make A5 pass that test by
    #: construction rather than by being a plausible packet. Non-empty for
    #: every case except C6 (whose oracle is the abstained packet, by
    #: design); every twin's oracle is abstained by design, so twins never
    #: set this field.
    oracle_text: str = ""
    #: The corpus tree (distractor count) this fixture is scored against
    #: (spec: "Every fixture and every packet SHALL record the corpus tree
    #: ... it belongs to"). Defaults to :data:`BASE_DISTRACTOR_COUNT`: every
    #: fixture but C9/T9 is meant to run on the unpadded tree; C9/T9
    #: override it to :data:`DEFAULT_DISTRACTOR_COUNT`. Micro-round finding:
    #: an earlier draft left this ``None`` for sixteen of eighteen fixtures,
    #: which both contradicted the spec's "every fixture" and let a tree
    #: guard checking only "not None and equal" pass None-vs-None or
    #: 200-vs-None vacuously.
    distractor_count: int | None = BASE_DISTRACTOR_COUNT

    def __post_init__(self) -> None:
        if self.expected_status not in EXPECTED_STATUSES:
            raise FixtureError(f"{self.case_id}: unknown expected_status {self.expected_status!r}")
        if not self.turn.strip():
            raise FixtureError(f"{self.case_id}: turn must not be empty")
        if not self.reminder_turn.strip():
            raise FixtureError(f"{self.case_id}: reminder_turn must not be empty")


FIXTURES: tuple[FixtureCase, ...] = (
    # -- C1 / T1: AI-usage complaint -------------------------------------
    FixtureCase(
        case_id="C1",
        pairs_with=None,
        turn="I keep hitting my AI usage limits again this week.",
        reminder_turn=(
            "Remember, I already logged that I keep hitting the weekly cap on "
            "this — factor that in."
        ),
        gold=("c1_subscriptions_collection", "c1_weekly_limit_insight", "c1_capacity_ceilings_pattern"),
        poison=("t1_fitness_goal_note",),
        roles=("historical_pattern", "capacity_constraint"),
        must_include=("weekly limit", "capacity ceiling"),
        must_exclude=(),
        expected_status="resolved",
        oracle_text=(
            "Your AI subscriptions collection shows you tend to hit the weekly "
            "limit before renewal, and the capacity-ceilings pattern note explains "
            "why: flat-rate plans scale by parallel pools, not a bigger per-call quota."
        ),
    ),
    FixtureCase(
        case_id="T1",
        pairs_with="C1",
        turn="I keep hitting my step count goal again this week.",
        reminder_turn="No, this one's just a fitness goal — nothing to pull in from tooling notes.",
        gold=(),
        poison=("c1_subscriptions_collection", "c1_weekly_limit_insight", "c1_capacity_ceilings_pattern"),
        roles=(),
        must_include=(),
        must_exclude=(),
        expected_status="unresolved",
    ),
    # -- C2 / T2: planning to cook ----------------------------------------
    FixtureCase(
        case_id="C2",
        pairs_with=None,
        turn="I'm planning to cook this recipe for the dinner on Saturday.",
        reminder_turn="I already looked at the grill equipment page for this — use that.",
        gold=("c2_grill_equipment_page", "c2_cooking_method_insight"),
        poison=("t2_camera_gear_note",),
        roles=("equipment_profile", "method_insight"),
        must_include=("grill", "cooking method"),
        must_exclude=(),
        expected_status="resolved",
        oracle_text=(
            "The grill equipment page describes a two-zone gas grill serviced this "
            "spring, and the cooking-method insight recommends indirect heat for "
            "this recipe."
        ),
    ),
    FixtureCase(
        case_id="T2",
        pairs_with="C2",
        turn="I'm planning to photograph this for the dinner on Saturday.",
        reminder_turn="No, this is about the camera gear, not any cooking equipment.",
        gold=(),
        poison=("c2_grill_equipment_page", "c2_cooking_method_insight"),
        roles=(),
        must_include=(),
        must_exclude=(),
        expected_status="unresolved",
    ),
    # -- C3 / T3: an implementation turn worded to avoid self-contamination
    FixtureCase(
        case_id="C3",
        pairs_with=None,
        turn="Let's move the next roadmap item forward and note the pointer to its design document.",
        reminder_turn="That roadmap item is tracked under this project's own Planning collection — use that pointer.",
        gold=("c3_planning_item", "c3_design_pointer"),
        poison=("t3_other_project_planning_item",),
        roles=("planning_item", "design_pointer"),
        must_include=("extending the reporting module",),
        must_exclude=(),
        expected_status="resolved",
        oracle_text=(
            "The next roadmap item is extending the reporting module, and its "
            "design pointer is the roadmap-item-A design note."
        ),
    ),
    FixtureCase(
        case_id="T3",
        pairs_with="C3",
        turn="Let's move the next roadmap item forward for the other workstream.",
        reminder_turn="That's the other workstream's own roadmap item — separate Planning collection from the first one.",
        gold=("t3_other_project_planning_item",),
        poison=("c3_planning_item", "c3_design_pointer"),
        roles=("planning_item",),
        must_include=(),
        must_exclude=(),
        expected_status="resolved",
    ),
    # -- C4 / T4: named/unnamed person -------------------------------------
    FixtureCase(
        case_id="C4",
        pairs_with=None,
        turn="My colleague who had the deployment issue last month followed up again.",
        reminder_turn="That's the colleague from the deployment-issue note — same person, no name needed.",
        gold=("c4_entity_profile", "c4_failure_note"),
        poison=("t4_shared_first_name_entity_a", "t4_shared_first_name_entity_b"),
        roles=("entity_profile", "failure_note"),
        must_include=("failed on the Mac build",),
        must_exclude=(),
        expected_status="resolved",
        oracle_text=(
            "That's the colleague whose deployment failed on the Mac build last "
            "month, root-caused to a config drift."
        ),
    ),
    FixtureCase(
        case_id="T4",
        pairs_with="C4",
        turn="Alex mentioned the deployment issue again.",
        reminder_turn="Which Alex — there are two on the team. Check before assuming either.",
        # Winnable (B2): T4's own narrow gold is the two same-kind person
        # candidates it is genuinely ambiguous between -- never C4's content.
        gold=("t4_shared_first_name_entity_a", "t4_shared_first_name_entity_b"),
        poison=("c4_entity_profile", "c4_failure_note"),
        roles=(),
        must_include=(),
        must_exclude=(),
        expected_status="ambiguous",
    ),
    # -- C5 / T5: resource unavailable per latest Records item -------------
    FixtureCase(
        case_id="C5",
        pairs_with=None,
        turn="Can I use the shared workshop bench for tomorrow's session?",
        reminder_turn="Check the workshop bench's records — it was marked unavailable last week.",
        gold=("c5_resource_profile", "c5_records_latest_unavailable"),
        poison=("t5_available_resource",),
        roles=("resource_profile", "current_state"),
        must_include=("unavailable",),
        must_exclude=(),
        expected_status="resolved",
        oracle_text="The workshop bench's records show it was marked unavailable for repair as of 2026-09-10, its latest entry.",
    ),
    FixtureCase(
        case_id="T5",
        pairs_with="C5",
        turn="Can I use the mobile scanner cart for tomorrow's session?",
        reminder_turn="That one's fine — no availability issue, unlike the bench.",
        gold=("t5_available_resource",),
        poison=("c5_records_latest_unavailable",),
        roles=("resource_profile",),
        must_include=(),
        must_exclude=("unavailable",),
        expected_status="resolved",
    ),
    # -- C6 / T6: no-memory turn --------------------------------------------
    FixtureCase(
        case_id="C6",
        pairs_with=None,
        turn="What's half of nineteen?",
        reminder_turn="There's nothing to look up here — that's a general question, not something from our history.",
        gold=(),
        poison=(),
        roles=(),
        must_include=("9.5",),
        must_exclude=(),
        expected_status="unresolved",
        # C6's oracle is the abstained packet by design (B6): left empty.
    ),
    FixtureCase(
        case_id="T6",
        pairs_with="C6",
        turn="Can you convert the grill's target temperature from Fahrenheit to Celsius?",
        reminder_turn="Still just a conversion — no need to pull in the grill's equipment page for this.",
        gold=(),
        poison=("c2_grill_equipment_page",),
        roles=(),
        must_include=(),
        must_exclude=(),
        expected_status="unresolved",
    ),
    # -- C7 / T7: ambiguous domain -------------------------------------------
    FixtureCase(
        case_id="C7",
        pairs_with=None,
        turn="Can you look into AI search for me?",
        reminder_turn="I meant the AI-search hub specifically, not general web search.",
        gold=("c7_hub_feature", "c7_hub_market", "c7_hub_search_ux"),
        poison=(),
        roles=("hub_context",),
        must_include=("AI search feature", "AI search market"),
        must_exclude=(),
        expected_status="ambiguous",
        oracle_text=(
            "\"AI search\" is ambiguous here between at least two senses: the "
            "in-app AI search feature implementation hub, and the AI search "
            "market-research hub comparing competing search engines."
        ),
    ),
    FixtureCase(
        case_id="T7",
        pairs_with="C7",
        turn="Can you look into the AI search feature implementation for me?",
        reminder_turn="Good, that's scoped enough — the feature hub, not the market or UX ones.",
        gold=("c7_hub_feature",),
        poison=("c7_hub_market", "c7_hub_search_ux"),
        roles=("hub_context",),
        must_include=(),
        must_exclude=(),
        expected_status="resolved",
    ),
    # -- C8 / T8: supersession chain -----------------------------------------
    FixtureCase(
        case_id="C8",
        pairs_with=None,
        turn="What's the current approach for the onboarding flow?",
        reminder_turn="Use the current onboarding approach, not either retired one.",
        gold=("c8_active_head",),
        poison=("c8_superseded_ancestor_1", "c8_superseded_ancestor_2"),
        roles=("current_state", "supersession"),
        must_include=("version 3",),
        must_exclude=("retired approach presented as current",),
        expected_status="resolved",
        oracle_text="The current onboarding approach is version 3; versions 1 and 2 are retired.",
    ),
    FixtureCase(
        case_id="T8",
        pairs_with="C8",
        turn="What's the current approach for the support rota?",
        reminder_turn="That one's never been revised, so there's nothing superseded to mark.",
        gold=("t8_unchained_active_note",),
        poison=("c8_superseded_ancestor_1", "c8_superseded_ancestor_2"),
        roles=("current_state",),
        must_include=(),
        must_exclude=("superseded",),
        expected_status="resolved",
    ),
    # -- C9 / T9: C2's turn and its twin's turn, both on a corpus tree padded
    # with ~200 domain-vocabulary distractors --------------------------------
    # N1 (round-two revision): C9 is the grill case's own turn (C2) scored
    # against the padded tree; T9 is C2's *twin's* own turn (T2, verbatim) on
    # that same padded tree -- a real negative control again (gold empty,
    # expected unresolved), restoring the ninth negative twin. Padding
    # robustness compares C9's padded-tree score against C2's own score on
    # the unpadded tree (`score_padding_robustness`), never against T9: T9's
    # job is to prove padding alone doesn't manufacture a false activation
    # for an unrelated query, which is a different property from "did
    # padding change the grill query's own precision/recall".
    FixtureCase(
        case_id="C9",
        pairs_with=None,
        turn="I'm planning to cook this recipe for the dinner on Saturday.",
        reminder_turn="I already looked at the grill equipment page for this — use that.",
        gold=("c2_grill_equipment_page", "c2_cooking_method_insight"),
        poison=("t2_camera_gear_note",),
        roles=("equipment_profile", "method_insight"),
        must_include=("grill", "cooking method"),
        must_exclude=(),
        expected_status="resolved",
        oracle_text=(
            "The grill equipment page describes a two-zone gas grill serviced this "
            "spring, and the cooking-method insight recommends indirect heat for "
            "this recipe."
        ),
        distractor_count=DEFAULT_DISTRACTOR_COUNT,
    ),
    FixtureCase(
        case_id="T9",
        pairs_with="C9",
        turn="I'm planning to photograph this for the dinner on Saturday.",
        reminder_turn="No, this is about the camera gear, not any cooking equipment.",
        gold=(),
        poison=("c2_grill_equipment_page", "c2_cooking_method_insight"),
        roles=(),
        must_include=(),
        must_exclude=(),
        expected_status="unresolved",
        distractor_count=DEFAULT_DISTRACTOR_COUNT,
    ),
)


#: Anchor kind per logical gold/poison key, for the deterministic scorer's
#: per-case-per-anchor-kind recall/precision (task 2). A key absent from this
#: mapping scores under kind ``"unknown"`` rather than raising, because a
#: locally-authored real-vault key mapping (never committed) may extend the
#: key space without touching this module.
KEY_KINDS: dict[str, str] = {
    "c1_subscriptions_collection": "records_collection",
    "c1_weekly_limit_insight": "note",
    "c1_capacity_ceilings_pattern": "note",
    "t1_fitness_goal_note": "note",
    "c2_grill_equipment_page": "equipment",
    "c2_cooking_method_insight": "note",
    "t2_camera_gear_note": "equipment",
    "c3_planning_item": "planning_item",
    "c3_design_pointer": "note",
    "t3_other_project_planning_item": "planning_item",
    "c4_entity_profile": "entity",
    "c4_failure_note": "note",
    "t4_shared_first_name_entity_a": "entity",
    "t4_shared_first_name_entity_b": "entity",
    "c5_resource_profile": "resource",
    "c5_records_latest_unavailable": "records_collection",
    "t5_available_resource": "resource",
    "c7_hub_feature": "hub",
    "c7_hub_market": "hub",
    "c7_hub_search_ux": "hub",
    "c8_superseded_ancestor_1": "note",
    "c8_superseded_ancestor_2": "note",
    "c8_active_head": "note",
    "t8_unchained_active_note": "note",
}


def anchor_kind_for(key: str) -> str:
    """The anchor kind for a logical gold/poison key; ``"unknown"`` if unmapped."""

    return KEY_KINDS.get(key, "unknown")


#: Short natural-language phrase(s) per logical gold/poison key (task 3, B5).
#: Used by the blind-extraction/gold-poison intersection: a realistic grader
#: reports *paraphrased facts*, never a fixture's internal logical keys, so
#: the intersection step matches on these phrases (normalised: casefold,
#: punctuation stripped, simple stemming), not on key identity. Almost every
#: key carries exactly one phrasing; ``c5_records_latest_unavailable``
#: carries two, pre-registered, because its negated shape ("not available")
#: is not reachable from its positive phrasing by stopword removal and
#: stemming alone -- "unavailable" and "available" share no root under this
#: module's stemmer, and inventing one would risk conflating them elsewhere
#: (micro-round finding: this is exactly the C5 gold/poison distinction the
#: fixture tests).
GOLD_POISON_FACTS: dict[str, tuple[str, ...]] = {
    "c1_subscriptions_collection": ("an AI subscriptions collection tracking plan tiers",),
    "c1_weekly_limit_insight": ("usage hits the weekly limit before renewal",),
    "c1_capacity_ceilings_pattern": ("a recurring capacity ceilings pattern on flat-rate plans",),
    "t1_fitness_goal_note": ("a step-count fitness goal, unrelated to tooling",),
    "c2_grill_equipment_page": ("a two-zone gas grill serviced this spring",),
    "c2_cooking_method_insight": ("indirect heat works best for this recipe",),
    "t2_camera_gear_note": ("a camera body and prime lens for photography",),
    "c3_planning_item": ("the next roadmap item extends the reporting module",),
    "c3_design_pointer": ("the design notes for that roadmap item",),
    "t3_other_project_planning_item": ("the other workstream's own roadmap item",),
    "c4_entity_profile": ("a colleague on the platform team",),
    "c4_failure_note": ("a deployment failed on the Mac build last month",),
    "t4_shared_first_name_entity_a": ("a colleague on the data team",),
    "t4_shared_first_name_entity_b": ("a colleague on the support team",),
    "c5_resource_profile": ("a shared workshop bench booked by session",),
    "c5_records_latest_unavailable": (
        "the bench was marked unavailable for repair",
        "the bench is not available",
    ),
    "t5_available_resource": ("a mobile scanner cart currently available",),
    "c7_hub_feature": ("the in-app AI search feature implementation hub",),
    "c7_hub_market": ("the AI search market-research hub",),
    "c7_hub_search_ux": ("the general search UX research hub",),
    "c8_superseded_ancestor_1": ("an earlier onboarding approach, since retired",),
    "c8_superseded_ancestor_2": ("a second onboarding approach, since retired",),
    "c8_active_head": ("the current onboarding approach",),
    "t8_unchained_active_note": ("the current support rota with no prior revisions",),
}


def fact_for(key: str) -> tuple[str, ...]:
    """The natural-language fact phrase(s) for a logical key; ``()`` if unmapped."""

    return GOLD_POISON_FACTS.get(key, ())


#: The naive-path latency constant (task 4.2), replacing the spec's
#: pre-registered p50 800 ms / p95 2,500 ms placeholder with a measured
#: figure. Measured on ``MEASURED_LATENCY_DATE`` on the personal cell
#: (quiesced, checked via /proc/<pid>/stat CPU deltas before the run):
#: `ask_memory` over ``MEASURED_LATENCY_SAMPLE_SIZE`` fixture turns, each
#: with a nonce appended, `mode="hybrid"`, `limit=5`, `detail="compact"`,
#: `include_timings=true`; the figure is each call's reported
#: `timings.total_ms` (server-side, excludes MCP transport). Nearest-rank
#: percentile (matching docs/benchmarks.md's own convention). Numeric-only
#: (minor fix): the sample size and date are separate constants so this dict
#: is honestly typed as ``dict[str, float]`` throughout, not a mix of floats
#: and strings.
MEASURED_LATENCY_SAMPLE_SIZE = 18
MEASURED_LATENCY_DATE = "2026-09-16"
MEASURED_LATENCY_MS: dict[str, float] = {
    "min_ms": 881.653,
    "p50_ms": 10744.641,
    "p95_ms": 16488.201,
    "max_ms": 16823.787,
    "mean_ms": 11197.24,
}


def fixture_by_id(case_id: str) -> FixtureCase:
    for fixture in FIXTURES:
        if fixture.case_id == case_id:
            return fixture
    raise FixtureError(f"unknown fixture case id {case_id!r}")


def cases(fixtures: tuple[FixtureCase, ...] = FIXTURES) -> tuple[FixtureCase, ...]:
    return tuple(f for f in fixtures if f.case_id in CASE_IDS)


def twins(fixtures: tuple[FixtureCase, ...] = FIXTURES) -> tuple[FixtureCase, ...]:
    return tuple(f for f in fixtures if f.case_id in TWIN_IDS)


def _canonical(fixture: FixtureCase) -> dict:
    return {
        "case_id": fixture.case_id,
        "pairs_with": fixture.pairs_with,
        "turn": fixture.turn,
        "reminder_turn": fixture.reminder_turn,
        "gold": list(fixture.gold),
        "poison": list(fixture.poison),
        "roles": list(fixture.roles),
        "must_include": list(fixture.must_include),
        "must_exclude": list(fixture.must_exclude),
        "expected_status": fixture.expected_status,
        "oracle_text": fixture.oracle_text,
        "distractor_count": fixture.distractor_count,
    }


def fixture_set_digest(fixtures: Iterable[FixtureCase] = FIXTURES) -> str:
    """Stable sha256 over the canonicalized fixture set, ordered by case_id.

    Any edit to any field -- gold and poison included -- changes this digest,
    which is what lets a run manifest bind to "the gold list a run's manifest
    referenced" and report void when the two disagree (see
    ``openspec/changes/add-context-activation-benchmark/specs/
    context-activation-benchmark/spec.md``, "Gold is authored before
    retrieval").
    """

    ordered = sorted((_canonical(f) for f in fixtures), key=lambda row: row["case_id"])
    blob = json.dumps(ordered, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


#: Twins carrying a *narrow* legitimate gold of their own (module docstring):
#: only these may have a non-empty gold that overlaps in kind with, but is
#: always disjoint from, their paired case's own gold. Every other twin's
#: gold must be fully disjoint from its case's gold -- for the eight with
#: empty gold this holds trivially, but the check is written to catch a
#: future edit that accidentally gives a twin outside this list gold drawn
#: from its own case.
NARROW_GOLD_TWIN_IDS: tuple[str, ...] = ("T3", "T4", "T5", "T7", "T8")


def assert_manifest_consistent(fixtures: tuple[FixtureCase, ...] = FIXTURES) -> None:
    """Refuse a fixture set that is not exactly nine cases paired with nine twins."""

    ids = [f.case_id for f in fixtures]
    if len(set(ids)) != len(ids):
        raise FixtureError("duplicate case_id in fixture set")
    if sorted(ids) != sorted(CASE_IDS + TWIN_IDS):
        raise FixtureError(f"fixture set must be exactly {CASE_IDS + TWIN_IDS}; got {sorted(ids)}")
    by_id = {f.case_id: f for f in fixtures}
    for twin_id in TWIN_IDS:
        twin = by_id[twin_id]
        if twin.pairs_with not in CASE_IDS:
            raise FixtureError(f"{twin_id}: pairs_with must name a case id, got {twin.pairs_with!r}")
        if twin.gold and twin_id not in NARROW_GOLD_TWIN_IDS:
            raise FixtureError(
                f"{twin_id}: carries gold {twin.gold} but is not in NARROW_GOLD_TWIN_IDS "
                f"{NARROW_GOLD_TWIN_IDS} -- every other twin's gold must stay empty"
            )
        if twin_id not in NARROW_GOLD_TWIN_IDS:
            # A narrow-gold twin is trusted to overlap its case's own gold on
            # purpose (T7's narrow gold is one of C7's own three ambiguous
            # candidates -- resolving to it is the whole point, not a false
            # activation); every other twin's gold must be disjoint.
            case = by_id[twin.pairs_with]
            if not set(twin.gold).isdisjoint(case.gold):
                raise FixtureError(
                    f"{twin_id}: gold {twin.gold} overlaps its paired case {case.case_id}'s own gold {case.gold}"
                )
    for case_id in CASE_IDS:
        case = by_id[case_id]
        if case.pairs_with is not None:
            raise FixtureError(f"{case_id}: a case must not carry pairs_with, got {case.pairs_with!r}")
    for fixture in fixtures:
        if fixture.distractor_count is None:
            raise FixtureError(
                f"{fixture.case_id}: distractor_count must be set (every fixture records its corpus tree)"
            )


#: Every non-empty turn and reminder turn in the fixture set, for the
#: corpus-contamination check (never checked in piecemeal: a leak in either
#: field is equally a leak).
ALL_TURNS: tuple[str, ...] = tuple(
    text for fixture in FIXTURES for text in (fixture.turn, fixture.reminder_turn) if text.strip()
)

#: Every gold/poison fact phrase (never `must_include`: those are
#: response-reflection facts, some of them content-free numeric answers like
#: C6's "9.5" that a naive normalised substring check would false-positive
#: on). Used only by :func:`find_fact_leaks_outside_gold_poison_pages` (N2),
#: scoped to non-gold-poison pages -- a gold/poison fact legitimately belongs
#: on its own gold or poison page, so checking every page indiscriminately
#: would misfire on the fixture's own intended content.
ALL_FACT_PHRASES: tuple[str, ...] = tuple(
    sorted({phrase for phrases in GOLD_POISON_FACTS.values() for phrase in phrases})
)


def find_verbatim_leaks(root: Path, *, turns: Iterable[str] = ALL_TURNS) -> tuple[tuple[str, str], ...]:
    """Every ``(relative page path, leaked turn)`` pair found verbatim under ``root``."""

    leaks: list[tuple[str, str]] = []
    for path in sorted(Path(root).rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        for turn in turns:
            if turn and turn in text:
                leaks.append((rel, turn))
    return tuple(leaks)


def _normalize_for_leak_check(text: str) -> str:
    """Casefold, collapse whitespace (incl. newlines), strip punctuation,
    and map common typographic variants to ASCII (N3): a line-wrapped quote
    or an em-dash rendering of a fixture turn is exactly as much a leak as a
    byte-identical one, and must not hide behind formatting.
    """

    normalized = unicodedata.normalize("NFKD", text)
    normalized = normalized.translate(
        str.maketrans({"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"'})
    )
    normalized = normalized.casefold()
    normalized = re.sub(r"[^\w\s]", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def find_normalized_leaks(root: Path, *, turns: Iterable[str] = ALL_TURNS) -> tuple[tuple[str, str], ...]:
    """Every ``(relative page path, leaked turn)`` pair found near-verbatim under ``root``.

    Catches a leak that survives casefolding, whitespace/newline collapse,
    punctuation stripping, and em/en-dash or curly-quote normalisation --
    exactly the class a line-wrapped quotation or a ``--``-for-em-dash
    rewrite would otherwise hide from :func:`find_verbatim_leaks`.
    """

    leaks: list[tuple[str, str]] = []
    normalized_terms = {turn: _normalize_for_leak_check(turn) for turn in turns if turn}
    for path in sorted(Path(root).rglob("*.md")):
        normalized_text = _normalize_for_leak_check(path.read_text(encoding="utf-8"))
        rel = path.relative_to(root).as_posix()
        for turn, normalized_turn in normalized_terms.items():
            if normalized_turn and normalized_turn in normalized_text:
                leaks.append((rel, turn))
    return tuple(leaks)


def find_fact_leaks_outside_gold_poison_pages(
    root: Path, key_to_path: dict[str, str], *, facts: Iterable[str] = ALL_FACT_PHRASES
) -> tuple[tuple[str, str], ...]:
    """Verbatim or near-verbatim gold/poison *fact* leakage into any page that
    is not itself one of the fixture's own designated gold/poison pages (N2).

    Distinct from :func:`find_verbatim_leaks`/:func:`find_normalized_leaks`
    (which check every page, gold/poison pages included, for a leaked
    *turn* -- something no corpus page, gold or otherwise, should ever
    contain): a gold/poison fact legitimately belongs on its own gold or
    poison page, so checking every page indiscriminately for fact leakage
    would misfire on the fixture's own intended content. Only distractor and
    neighbourhood pages, which carry no such license, are checked here.
    """

    gold_poison_pages = set(key_to_path.values())
    leaks: list[tuple[str, str]] = []
    normalized_facts = {fact: _normalize_for_leak_check(fact) for fact in facts if fact}
    for path in sorted(Path(root).rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if rel in gold_poison_pages:
            continue
        text = path.read_text(encoding="utf-8")
        normalized_text = _normalize_for_leak_check(text)
        for fact, normalized_fact in normalized_facts.items():
            if fact in text or (normalized_fact and normalized_fact in normalized_text):
                leaks.append((rel, fact))
    return tuple(leaks)


# --------------------------------------------------------------------------
# The seeded synthetic corpus.
# --------------------------------------------------------------------------


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _page(
    root: Path,
    rel: str,
    title: str,
    body: str,
    *,
    status: str = "active",
    extra_frontmatter: str = "",
    superseded_by: str | None = None,
) -> str:
    frontmatter = f"status: {status}\n{extra_frontmatter}"
    if superseded_by:
        frontmatter += f"superseded_by: {superseded_by}\n"
    _write(
        root,
        rel,
        f"---\ntitle: {title}\n{frontmatter}updated: 2026-09-01\n---\n\n# {title}\n\n{body}\n",
    )
    return rel


@dataclass(frozen=True)
class StateSourceDeclaration:
    """Fixture-authored permission for one canonical ``#current`` projection."""

    anchor_key: str
    source_key: str
    source_kind: str
    state_field: str
    projection_suffix: str = "#current"


# These are benchmark facts, not a generic product ontology.  Each declaration
# is checked against the freshly written canonical pages before it can become a
# scoring binding.
STATE_SOURCE_DECLARATIONS: tuple[StateSourceDeclaration, ...] = (
    StateSourceDeclaration("c1_subscriptions_collection", "c1_subscriptions_collection", "records", "state"),
    StateSourceDeclaration("c2_grill_equipment_page", "c2_grill_equipment_page", "profile", "status"),
    StateSourceDeclaration("t2_camera_gear_note", "t2_camera_gear_note", "profile", "status"),
    StateSourceDeclaration("c5_resource_profile", "c5_records_latest_unavailable", "records", "status"),
    StateSourceDeclaration(
        "c5_records_latest_unavailable", "c5_records_latest_unavailable", "records", "status"
    ),
    StateSourceDeclaration("t5_available_resource", "t5_available_resource", "profile", "status"),
)


@dataclass(frozen=True)
class CorpusManifest:
    corpus_id: str
    seed: int
    distractor_count: int
    key_to_path: dict[str, str]
    corpus_hash: str
    logical_hash: str
    state_sources: tuple[StateSourceDeclaration, ...]


@dataclass(frozen=True)
class ProjectionBinding:
    """One canonically read projection and its independent packet identity."""

    anchor_key: str
    anchor_ref: str
    source_key: str
    source_ref: str
    source_kind: str
    state_field: str
    projection_ref: str
    statement: str
    as_of: str
    source_path: str
    source_snapshot_digest: str


@dataclass(frozen=True)
class ReferenceBinding:
    """Frozen benchmark reference map and verified current-state projections."""

    corpus_digest: str
    logical_corpus_digest: str
    key_to_ref: tuple[tuple[str, str], ...]
    mapping_digest: str
    projections: tuple[ProjectionBinding, ...]
    digest: str


def _corpus_hash(root: Path) -> str:
    """Digest exact canonical corpus bytes, excluding derived navigation/logs."""

    digest = hashlib.sha256()
    kb_root = root / "Knowledge Base"
    for path in sorted(candidate for candidate in kb_root.rglob("*") if candidate.is_file()):
        if path.suffix.lower() not in {".json", ".md", ".yaml", ".yml"}:
            continue
        if path.name in {"index.md", "log.md"}:
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _logical_corpus_hash(
    root: Path,
    *,
    seed: int,
    distractor_count: int,
    key_to_path: dict[str, str],
    state_sources: tuple[StateSourceDeclaration, ...] = STATE_SOURCE_DECLARATIONS,
) -> str:
    """Digest stable fixture semantics separately from concrete write receipts.

    Canonical note/entity writers mint their own ids, and Records/Planning
    append audit receipts.  Those concrete values remain covered by
    :func:`_corpus_hash`.  This identity covers the declared fixture inputs,
    paths, and authored semantic projections so independently constructed
    corpora with the same seed remain comparable.
    """

    pages: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.md")):
        relative = path.relative_to(root).as_posix()
        if (
            relative.startswith(".exomem/")
            or relative.startswith("Knowledge Base/_Schema/")
            or relative.startswith("Knowledge Base/_Governance/")
            or path.name in {"index.md", "log.md"}
        ):
            continue
        text = path.read_text(encoding="utf-8")
        frontmatter = text.split("---", 2)[1] if text.startswith("---\n") else ""
        governed_type = ""
        match = re.search(r"(?m)^type:\s*([^\n]+)$", frontmatter)
        if match:
            governed_type = match.group(1).strip()
        if governed_type in {"entity", "failure", "insight", "pattern"}:
            text = re.sub(r"(?m)^exomem_id:\s*[^\n]+\n", "", text, count=1)
        if governed_type == "note" and re.search(r"(?m)^tags:.*\bresource\b", frontmatter):
            text = re.sub(r"(?m)^(?:created|updated):\s*[^\n]+\n", "", text)
        text = re.sub(r"(?m)^(?:plan|record)_audit:\s*[^\n]+\n", "", text)
        text = re.sub(r"(?m)^# exomem-(?:plan|record)-audit:\s*[^\n]+\n", "", text)
        text = re.sub(
            r"(?m)^<!-- exomem-item-presentation:v1 [^\n]+ -->\n",
            "<!-- exomem-item-presentation:v1 -->\n",
            text,
        )
        pages.append((relative, text))
    payload = {
        "corpus_id": CORPUS_ID,
        "fixture_set_hash": fixture_set_digest(),
        "seed": seed,
        "distractor_count": distractor_count,
        "key_to_path": dict(sorted(key_to_path.items())),
        "state_sources": [asdict(item) for item in state_sources],
        "pages": pages,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_snapshot_digest(root: Path, paths: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(set(paths)):
        path = root / relative
        if not path.is_file():
            raise FixtureError(f"declared state source is missing canonical file {relative}")
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _page_frontmatter(root: Path, relative: str) -> dict[str, object]:
    from exomem import vault

    path = root / relative
    try:
        frontmatter, _body, marker = vault.parse_frontmatter(
            path.read_text(encoding="utf-8"), strict=True
        )
    except (OSError, vault.FrontmatterError) as error:
        raise FixtureError(f"cannot read declared state page {relative}: {error}") from error
    if marker is None or not isinstance(frontmatter, dict):
        raise FixtureError(f"declared state page has no canonical frontmatter: {relative}")
    return frontmatter


def _page_title(root: Path, relative: str, frontmatter: Mapping[str, object]) -> str:
    title = str(frontmatter.get("title") or "").strip()
    if title:
        return title
    text = (root / relative).read_text(encoding="utf-8")
    match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    if not match:
        raise FixtureError(f"declared state anchor has no authored title: {relative}")
    return match.group(1).strip()


def _read_declared_projection(
    root: Path,
    manifest: CorpusManifest,
    declaration: StateSourceDeclaration,
    key_to_ref: dict[str, str],
) -> ProjectionBinding:
    anchor_path = manifest.key_to_path[declaration.anchor_key]
    source_path = manifest.key_to_path[declaration.source_key]
    anchor_ref = key_to_ref[declaration.anchor_key]
    source_ref = key_to_ref[declaration.source_key]

    if declaration.projection_suffix != "#current":
        raise FixtureError(f"unsupported projection form for {declaration.anchor_key}")
    if declaration.source_kind == "profile":
        if anchor_path != source_path:
            raise FixtureError(f"profile state source must be its own anchor: {declaration.anchor_key}")
        frontmatter = _page_frontmatter(root, source_path)
        value = frontmatter.get(declaration.state_field)
        if not isinstance(value, (str, int, float)) or not str(value).strip():
            raise FixtureError(
                f"profile state field {declaration.state_field!r} is absent for {declaration.anchor_key}"
            )
        statement = f"{declaration.state_field}: {str(value).strip()}"
        as_of = str(frontmatter.get("updated") or "")
        snapshot_paths = (source_path,)
    elif declaration.source_kind == "records":
        from exomem import record_governance, structured_collections

        collection = structured_collections.load_manifest(root, source_path)
        if collection.path != source_path:
            raise FixtureError(
                f"declared Records source resolved to a different canonical path: {collection.path}"
            )
        if collection.semantic_profile != "records":
            raise FixtureError(f"declared Records source is not records: {source_path}")
        if declaration.state_field not in collection.schema.fields:
            raise FixtureError(
                f"Records state field {declaration.state_field!r} is absent for {declaration.anchor_key}"
            )
        anchor_frontmatter = _page_frontmatter(root, anchor_path)
        anchor_title = _page_title(root, anchor_path, anchor_frontmatter)
        claims = {
            _normalize_for_leak_check(term)
            for terms in (collection.claims or {}).values()
            for term in terms
        }
        if anchor_path != source_path and _normalize_for_leak_check(anchor_title) not in claims:
            raise FixtureError(f"declared Records source does not claim {declaration.anchor_key}")
        if "observed_on" not in collection.schema.fields:
            raise FixtureError(f"Records source has no observed_on field: {source_path}")
        result = record_governance.query_collection(
            root,
            collection,
            semantic_profile="records",
            sort_by="observed_on",
            descending=True,
            limit=1,
        )
        rows = tuple(result.rows)
        if not rows or not isinstance(rows[0], Mapping):
            raise FixtureError(f"Records source has no current authored state for {declaration.anchor_key}")
        row = rows[0]
        if (
            result.collection_id != collection.collection_id
            or row.get("collection_id") != collection.collection_id
            or not row.get("record_id")
            or row.get("inferred") is not False
            or row.get("ambiguous") is not False
        ):
            raise FixtureError(f"Records source returned an unproven item identity for {declaration.anchor_key}")
        value = row.get(declaration.state_field)
        if not isinstance(value, (str, int, float)) or not str(value).strip():
            raise FixtureError(
                f"Records source has no authored {declaration.state_field!r} value for {declaration.anchor_key}"
            )
        statement = f"{declaration.state_field}: {str(value).strip()}"
        as_of = str(row.get("observed_on") or "")
        snapshot_paths = tuple(version.path for version in result.source_versions)
    else:
        raise FixtureError(f"unknown declared state source kind {declaration.source_kind!r}")

    return ProjectionBinding(
        anchor_key=declaration.anchor_key,
        anchor_ref=anchor_ref,
        source_key=declaration.source_key,
        source_ref=source_ref,
        source_kind=declaration.source_kind,
        state_field=declaration.state_field,
        projection_ref=f"{anchor_ref}{declaration.projection_suffix}",
        statement=statement,
        as_of=as_of,
        source_path=source_path,
        source_snapshot_digest=_source_snapshot_digest(root, snapshot_paths),
    )


def reference_binding_digests(
    *,
    corpus_digest: str,
    logical_corpus_digest: str,
    key_to_ref: tuple[tuple[str, str], ...],
    projections: tuple[ProjectionBinding, ...],
) -> tuple[str, str]:
    """Return the mapping digest and exact frozen-binding digest."""

    mapping_digest = _json_digest(dict(key_to_ref))
    projection_rows = [
        {
            "anchor_key": item.anchor_key,
            "anchor_ref": item.anchor_ref,
            "source_key": item.source_key,
            "source_ref": item.source_ref,
            "source_kind": item.source_kind,
            "state_field": item.state_field,
            "projection_ref": item.projection_ref,
            "statement": item.statement,
            "source_path": item.source_path,
        }
        for item in projections
    ]
    digest = _json_digest(
        {
            "corpus_digest": corpus_digest,
            "logical_corpus_digest": logical_corpus_digest,
            "mapping_digest": mapping_digest,
            "projections": projection_rows,
            "source_snapshots": [
                (item.anchor_key, item.as_of, item.source_snapshot_digest) for item in projections
            ],
        }
    )
    return mapping_digest, digest


def freeze_reference_binding(
    root: Path,
    manifest: CorpusManifest,
    key_to_ref: dict[str, str],
) -> ReferenceBinding:
    """Freeze trusted refs and verified authored projection state before activation."""

    root = Path(root)
    if _corpus_hash(root) != manifest.corpus_hash:
        raise FixtureError("cannot freeze reference binding: corpus digest is stale")
    logical_hash = _logical_corpus_hash(
        root,
        seed=manifest.seed,
        distractor_count=manifest.distractor_count,
        key_to_path=manifest.key_to_path,
        state_sources=manifest.state_sources,
    )
    if logical_hash != manifest.logical_hash:
        raise FixtureError("cannot freeze reference binding: logical corpus digest is stale")
    if manifest.corpus_id != CORPUS_ID or manifest.state_sources != STATE_SOURCE_DECLARATIONS:
        raise FixtureError("cannot freeze reference binding: state-source declaration set is stale")

    required_keys = {key for fixture in FIXTURES for key in (*fixture.gold, *fixture.poison)}
    required_keys.update(
        key for declaration in manifest.state_sources for key in (declaration.anchor_key, declaration.source_key)
    )
    missing = sorted(key for key in required_keys if not isinstance(key_to_ref.get(key), str) or not key_to_ref[key])
    if missing:
        raise FixtureError(f"cannot freeze reference binding: missing trusted refs {missing}")
    frozen_map = tuple(sorted((key, key_to_ref[key]) for key in required_keys))
    projections = tuple(
        _read_declared_projection(root, manifest, declaration, dict(frozen_map))
        for declaration in manifest.state_sources
    )
    mapping_digest, digest = reference_binding_digests(
        corpus_digest=manifest.corpus_hash,
        logical_corpus_digest=manifest.logical_hash,
        key_to_ref=frozen_map,
        projections=projections,
    )
    return ReferenceBinding(
        corpus_digest=manifest.corpus_hash,
        logical_corpus_digest=manifest.logical_hash,
        key_to_ref=frozen_map,
        mapping_digest=mapping_digest,
        projections=projections,
        digest=digest,
    )


_CORPUS_DATE = date(2026, 9, 1)


def _compiled_note(
    root: Path,
    *,
    title: str,
    slug: str,
    observation: str,
    category: str = "finding",
    tags: tuple[str, ...] = (),
    extra_body: str = "",
    note_type: str = "insight",
) -> str:
    """Create one fixture note through the canonical compiled-note writer."""

    from exomem import note

    tag_text = "" if not tags else " " + " ".join(f"#{tag}" for tag in tags)
    body = (
        f"## Observations\n\n- [{category}] {observation}{tag_text}\n"
        + (f"\n{extra_body.strip()}\n" if extra_body.strip() else "")
    )
    arguments = {
        "vault_root": root,
        "content": body,
        "note_type": note_type,
        "title": title,
        "slug": slug,
        "sources": [],
        "tags": list(tags),
        "today": _CORPUS_DATE,
    }
    validation = note.note(validate_only=True, **arguments)
    reviewed: dict[str, object] = {}
    if getattr(validation.creation_validation, "reviewed_none_required", False):
        reviewed = {
            "relation_disposition": "reviewed_none",
            "relation_review_hash": validation.draft_hash,
            "relation_review_reason": "No honest relation exists in the synthetic fixture corpus.",
        }
    result = note.note(
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        **reviewed,
        **arguments,
    )
    return result.path


def _replace_note(root: Path, *, old_path: str, title: str, slug: str, observation: str) -> str:
    """Supersede one fixture note through the reviewed canonical writer."""

    from exomem import replace

    arguments = {
        "vault_root": root,
        "old_path": old_path,
        "reason": "replace the benchmark onboarding approach",
        "today": _CORPUS_DATE,
        "content": f"## Observations\n\n- [current state] {observation} #onboarding\n",
        "note_type": "insight",
        "title": title,
        "slug": slug,
        "sources": [],
        "tags": ["onboarding"],
    }
    validation = replace.replace(validate_only=True, **arguments)
    reviewed = {}
    if getattr(validation.creation_validation, "reviewed_none_required", False):
        reviewed = {
            "relation_disposition": "reviewed_none",
            "relation_review_hash": validation.draft_hash,
            "relation_review_reason": "The supersession is the fixture's truthful relation.",
        }
    result = replace.replace(
        draft_id=validation.draft_id,
        draft_hash=validation.draft_hash,
        draft_token=validation.draft_token,
        **reviewed,
        **arguments,
    )
    return result.new_path


def _entity(
    root: Path,
    *,
    name: str,
    slug: str,
    summary: str,
    tags: tuple[str, ...] = (),
) -> str:
    """Create one person fixture through the canonical entity writer."""

    from exomem import link

    return link.link(
        root,
        entity_type="person",
        name=name,
        slug=slug,
        summary=summary,
        tags=list(tags),
        today=_CORPUS_DATE,
    ).path


def _governed_resource(
    root: Path,
    *,
    path: str,
    title: str,
    observation: str,
    tags: tuple[str, ...],
    extra_body: str = "",
) -> str:
    """Create one curated resource through the supported generic writer."""

    from exomem.commands import op_manage_memory_file

    tag_text = " ".join(f"#{tag}" for tag in tags)
    content = (
        f"# {title}\n\n## Observations\n\n- [resource] {observation} {tag_text}\n"
        + (f"\n{extra_body.strip()}\n" if extra_body.strip() else "")
    )
    arguments = {
        "operation": "create",
        "path": path,
        "content": content,
        "frontmatter": {"type": "note", "status": "active", "tags": list(tags)},
    }
    validation = op_manage_memory_file(root, validate_only=True, **arguments)
    result = op_manage_memory_file(
        root,
        draft_id=validation.get("draft_id"),
        draft_hash=validation.get("draft_hash"),
        draft_token=validation.get("draft_token"),
        **arguments,
    )
    creation = result.get("creation") if isinstance(result, dict) else None
    if not isinstance(creation, dict) or creation.get("mutated") is not True:
        raise FixtureError(f"canonical resource writer did not commit {path}")
    if not (root / path).is_file():
        raise FixtureError(f"canonical resource writer omitted {path}")
    return path


def _records_manifest(
    *,
    exomem_id: str,
    title: str,
    source: str,
    claims: tuple[str, ...],
    fields: str,
    natural_key: str,
) -> str:
    claim_rows = ", ".join(claims)
    return f"""---
type: collection
exomem_id: {exomem_id}
title: {title}
semantic_profile: records
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: {source}
  format_version: 1
claims:
  terms: [{claim_rows}]
item_schema:
  natural_key: [{natural_key}]
  fields:
{fields}
---

Synthetic observed state for the context-activation corpus.
"""


def _create_records_collection(
    root: Path,
    *,
    manifest_path: str,
    manifest_text: str,
    items: tuple[tuple[str, dict[str, object]], ...],
) -> str:
    """Create and populate Records through the public product command."""

    from exomem.commands import op_record_memory

    op_record_memory(
        root,
        action="create",
        manifest_path=manifest_path,
        manifest_text=manifest_text,
        scaffold=True,
        why="seed the context-activation benchmark collection",
    )
    for item_key, item in items:
        snapshot = op_record_memory(root, action="inspect", collection=manifest_path)["snapshot"]
        op_record_memory(
            root,
            action="append",
            collection=manifest_path,
            item=item,
            item_key=item_key,
            expected_container_hash=snapshot,
            why="seed one observed benchmark state",
        )
    return manifest_path


def _planning_manifest(*, exomem_id: str, title: str) -> str:
    return f"""---
type: collection
exomem_id: {exomem_id}
title: {title}
semantic_profile: planning
collection_version: 1
schema_version: 1
lifecycle: active
storage:
  strategy: markdown-items
  source: Items
  format_version: 1
item_schema:
  natural_key: [title]
  fields:
    title:
      type: string
      required: true
    kind:
      type: string
    status:
      type: string
    lifecycle:
      type: string
    priority:
      type: string
    commitment:
      type: string
    horizon:
      type: string
    health:
      type: string
    area:
      type: string
    parent:
      type: string
    pointer:
      type: link
item_filename:
  version: 1
  fields: [title]
item_presentation:
  version: 1
  title: title
  summary: [kind, status, lifecycle, priority, commitment, horizon, pointer]
---

Synthetic intended work for the context-activation corpus.
"""


def _create_planning_item(
    root: Path,
    *,
    manifest_path: str,
    manifest_text: str,
    plan_id: str,
    title: str,
    pointer: str | None = None,
) -> str:
    """Create one collection and item through the canonical Planning writer."""

    from exomem import planning

    planning.create_collection(
        root,
        manifest_path,
        manifest_text,
        why="seed the context-activation benchmark plan",
    )
    item: dict[str, object] = {
        "title": title,
        "kind": "outcome",
        "status": "candidate",
        "lifecycle": "active",
    }
    if pointer is not None:
        item["pointer"] = pointer
    receipt = planning.add(
        root,
        manifest_path,
        plan_id=plan_id,
        item=item,
        why="seed one intended benchmark outcome",
    )
    return str(receipt["affected_paths"][0])


#: Distractor vocabulary for C9's padded neighbourhood (N2): generic,
#: generated-domain words adjacent to C2/C9's grilling content, so the
#: padding is realistically *near* the gold content rather than inert noise
#: -- a real discriminator has to do better than "ignore everything that
#: isn't verbatim". Deliberately excludes "grill" and "cooking method"
#: themselves: those are C2/C9's own `must_include` terms, and the leak
#: checks below refuse any distractor that reproduces a must_include fact.
_DISTRACTOR_WORD_BANK: tuple[str, ...] = (
    "smoke",
    "brine",
    "sear",
    "temperature",
    "wood",
    "doneness",
    "marinade",
    "thermometer",
    "charcoal",
    "basting",
    "resting",
    "rub",
    "coals",
    "drip pan",
    "wood chips",
)


def _distractor_body(rng: random.Random, index: int) -> str:
    words = rng.sample(_DISTRACTOR_WORD_BANK, k=4)
    return (
        f"Background synthetic transcript material, entry {index:04d}. "
        f"Generic outdoor-cooking notes touching on {words[0]}, {words[1]}, "
        f"{words[2]} and {words[3]}, from an unrelated session."
    )


def _build_corpus_in_process(
    root: Path, *, seed: int = 20260916, distractor_count: int = DEFAULT_DISTRACTOR_COUNT
) -> CorpusManifest:
    """Render the seeded synthetic corpus under ``root``.

    Generated names only -- no personal names, products, hosts or
    vault-structure labels. Refuses (:class:`FixtureError`) if any fixture
    turn, reminder turn, or authored fact leaked verbatim or near-verbatim
    into a rendered page, which is exactly the contamination class
    ``design.md`` recorded against the real vault (2026-09-16 reproduction).
    """

    from exomem.init import init_vault

    root = Path(root)
    init_vault(root)
    rng = random.Random(seed)
    key_to_path: dict[str, str] = {}

    # C1/T1 -- AI-usage complaint vs. an unrelated fitness goal.
    key_to_path["c1_subscriptions_collection"] = _create_records_collection(
        root,
        manifest_path="Knowledge Base/Records/AI Subscriptions/_collection.md",
        manifest_text=_records_manifest(
            exomem_id="10000000-0000-4000-8000-000000000001",
            title="AI subscriptions",
            source="Items",
            claims=("ai subscriptions", "usage limits", "weekly cap", "plan tiers"),
            natural_key="observed_on, subscription",
            fields=(
                "    observed_on:\n"
                "      type: date\n"
                "      required: true\n"
                "    subscription:\n"
                "      type: string\n"
                "      required: true\n"
                "    state:\n"
                "      type: string\n"
                "    note:\n"
                "      type: string"
            ),
        ),
        items=(
            (
                "10000000-0000-4000-8000-000000000011",
                {
                    "observed_on": "2026-09-01",
                    "subscription": "assistant-plan-alpha",
                    "state": "limit-reached",
                    "note": "Weekly capacity was exhausted before the plan renewed.",
                },
            ),
        ),
    )
    key_to_path["c1_weekly_limit_insight"] = _compiled_note(
        root,
        title="Weekly limit insight",
        slug="weekly-limit-insight",
        observation="Usage hits the weekly limit before renewal on the current tier.",
        category="operating constraint",
        tags=("capacity", "subscriptions"),
    )
    key_to_path["c1_capacity_ceilings_pattern"] = _compiled_note(
        root,
        title="Capacity ceilings pattern",
        slug="capacity-ceilings-pattern",
        observation="Flat-rate plans repeatedly reach their capacity ceilings before renewal.",
        category="pattern",
        tags=("capacity", "subscriptions"),
        note_type="pattern",
    )
    key_to_path["t1_fitness_goal_note"] = _compiled_note(
        root,
        title="Fitness goal note",
        slug="fitness-goal-note",
        observation="A step-count fitness goal is tracked separately from tooling.",
        category="goal",
        tags=("fitness",),
    )

    # C2/T2/C9/T9 -- planning to cook vs. photographing; C9/T9 reuse these
    # same pages, scored against the padded and unpadded trees respectively.
    key_to_path["c2_grill_equipment_page"] = _governed_resource(
        root,
        path="Knowledge Base/Products/Grill equipment.md",
        title="Grill equipment page",
        observation="A two-zone gas grill was serviced this spring.",
        tags=("resource", "equipment", "cooking"),
        extra_body="## Constraints\n\nUse indirect heat for long recipes.",
    )
    key_to_path["c2_cooking_method_insight"] = _compiled_note(
        root,
        title="Cooking method insight",
        slug="cooking-method-insight",
        observation="Indirect heat works best for this recipe on the grill.",
        category="method",
        tags=("cooking", "grill"),
    )
    key_to_path["t2_camera_gear_note"] = _governed_resource(
        root,
        path="Knowledge Base/Products/Camera gear.md",
        title="Camera gear note",
        observation="A camera body and prime lens are kept for photography.",
        tags=("resource", "equipment", "photography"),
    )

    # C3/T3 -- a roadmap item and its design pointer, twinned across an
    # unrelated second project's own roadmap item.
    key_to_path["c3_design_pointer"] = _compiled_note(
        root,
        title="Roadmap item A design pointer",
        slug="roadmap-item-a-design",
        observation="The design notes specify how the reporting module is extended.",
        category="design",
        tags=("roadmap", "reporting"),
    )
    key_to_path["c3_planning_item"] = _create_planning_item(
        root,
        manifest_path="Knowledge Base/Planning/Roadmap/_collection.md",
        manifest_text=_planning_manifest(
            exomem_id="30000000-0000-4000-8000-000000000001",
            title="Reporting roadmap",
        ),
        plan_id="30000000-0000-4000-8000-000000000011",
        title="Extending the reporting module",
        pointer=f"[[{key_to_path['c3_design_pointer'].removesuffix('.md')}]]",
    )
    key_to_path["t3_other_project_planning_item"] = _create_planning_item(
        root,
        manifest_path="Knowledge Base/Planning/Other Workstream/_collection.md",
        manifest_text=_planning_manifest(
            exomem_id="30000000-0000-4000-8000-000000000002",
            title="Other workstream roadmap",
        ),
        plan_id="30000000-0000-4000-8000-000000000012",
        title="Other workstream roadmap item",
    )

    # C4/T4 -- a named colleague and failure note vs. two entities sharing
    # one first name.
    key_to_path["c4_entity_profile"] = _entity(
        root,
        name="Rowan Ashfield",
        slug="rowan-ashfield",
        summary="A colleague on the platform team.",
        tags=("platform",),
    )
    key_to_path["c4_failure_note"] = _compiled_note(
        root,
        title="Deployment issue note",
        slug="deployment-issue-rowan",
        observation="Rowan Ashfield's deployment failed on the Mac build last month because of config drift.",
        category="failure",
        tags=("deployment", "platform"),
        extra_body=(
            "## Context\n\n"
            f"Affected colleague: [[{key_to_path['c4_entity_profile'].removesuffix('.md')}]]."
        ),
        note_type="failure",
    )
    key_to_path["t4_shared_first_name_entity_a"] = _entity(
        root,
        name="Alex Monroe",
        slug="alex-monroe",
        summary="A colleague on the data team.",
        tags=("data",),
    )
    key_to_path["t4_shared_first_name_entity_b"] = _entity(
        root,
        name="Alex Park",
        slug="alex-park",
        summary="A colleague on the support team.",
        tags=("support",),
    )

    # C5/T5 -- a resource made unavailable by its Records collection's
    # latest item (by date, never by line order -- N supplement), vs. an
    # available resource that must not be mislabeled.
    key_to_path["c5_resource_profile"] = _governed_resource(
        root,
        path="Knowledge Base/Systems/Workshop bench.md",
        title="Workshop bench",
        observation="A shared workshop bench is booked by session.",
        tags=("resource", "workshop"),
        extra_body=(
            "## Current state\n\n"
            "See [[Knowledge Base/Records/Workshop Bench/_collection]] for status."
        ),
    )
    key_to_path["c5_records_latest_unavailable"] = _create_records_collection(
        root,
        manifest_path="Knowledge Base/Records/Workshop Bench/_collection.md",
        manifest_text=_records_manifest(
            exomem_id="50000000-0000-4000-8000-000000000001",
            title="Workshop bench availability",
            source="Items",
            claims=("workshop bench", "availability", "repair", "shared resource"),
            natural_key="observed_on, resource",
            fields=(
                "    observed_on:\n"
                "      type: date\n"
                "      required: true\n"
                "    resource:\n"
                "      type: string\n"
                "      required: true\n"
                "    status:\n"
                "      type: enum\n"
                "      enum: [available, unavailable]\n"
                "    note:\n"
                "      type: string"
            ),
        ),
        items=(
            (
                "50000000-0000-4000-8000-000000000011",
                {
                    "observed_on": "2026-08-02",
                    "resource": "workshop-bench",
                    "status": "available",
                    "note": "Released for booked sessions.",
                },
            ),
            (
                "50000000-0000-4000-8000-000000000012",
                {
                    "observed_on": "2026-09-10",
                    "resource": "workshop-bench",
                    "status": "unavailable",
                    "note": "Held for repair.",
                },
            ),
        ),
    )
    key_to_path["t5_available_resource"] = _governed_resource(
        root,
        path="Knowledge Base/Systems/Scanner cart.md",
        title="Scanner cart",
        observation="A mobile scanner cart is currently available with no open issues.",
        tags=("resource", "scanning"),
    )

    # C6/T6 -- no pages are seeded for the no-memory turn itself: it must
    # resolve nothing. T6 mentions the C2 grill only lexically.

    # C7/T7 -- an ambiguous domain triple with distinct, pairwise-disjoint
    # wikilink neighbourhoods (N supplement): three pages each hub links to,
    # shared by no other hub, so "disjoint neighbourhoods" is a real,
    # checkable property rather than an assertion about empty link sets.
    neighbours: dict[str, list[str]] = {"feature": [], "market": [], "ux": []}
    for family, label in (("feature", "Feature"), ("market", "Market"), ("ux", "UX")):
        for suffix, blurb in (
            ("a", "reference material"),
            ("b", "a design note"),
            ("c", "a status update"),
        ):
            neighbours[family].append(
                _compiled_note(
                    root,
                    title=f"{label} neighbour {suffix}",
                    slug=f"{family}-neighbour-{suffix}",
                    observation=f"Synthetic {family} {blurb} for a distinct hub neighbourhood.",
                    category="reference",
                    tags=(family,),
                )
            )

    def neighbour_links(family: str) -> str:
        return ", ".join(f"[[{path.removesuffix('.md')}]]" for path in neighbours[family])

    key_to_path["c7_hub_feature"] = _compiled_note(
        root,
        title="AI search feature hub",
        slug="ai-search-feature-hub",
        observation="The in-app AI search feature implementation hub owns product delivery context.",
        category="hub",
        tags=("hub", "ai-search", "feature"),
        extra_body=f"## References\n\n{neighbour_links('feature')}.",
    )
    key_to_path["c7_hub_market"] = _compiled_note(
        root,
        title="AI search market hub",
        slug="ai-search-market-hub",
        observation="The AI search market-research hub compares competing search engines.",
        category="hub",
        tags=("hub", "ai-search", "market"),
        extra_body=f"## References\n\n{neighbour_links('market')}.",
    )
    key_to_path["c7_hub_search_ux"] = _compiled_note(
        root,
        title="Search UX hub",
        slug="search-ux-hub",
        observation="The general search UX research hub studies result presentation.",
        category="hub",
        tags=("hub", "search", "ux"),
        extra_body=f"## References\n\n{neighbour_links('ux')}.",
    )

    # C8/T8 -- a supersession chain (two retired ancestors, one active
    # head), vs. a single note with no revision history at all.
    first = _compiled_note(
        root,
        title="Onboarding approach v1",
        slug="onboarding-approach-v1",
        observation="The first onboarding approach used a long guided checklist.",
        category="method",
        tags=("onboarding",),
    )
    second = _replace_note(
        root,
        old_path=first,
        title="Onboarding approach v2",
        slug="onboarding-approach-v2",
        observation="The second onboarding approach used a shorter guided checklist.",
    )
    third = _replace_note(
        root,
        old_path=second,
        title="Onboarding approach v3",
        slug="onboarding-approach-v3",
        observation="The current onboarding approach is version 3.",
    )
    key_to_path["c8_superseded_ancestor_1"] = first
    key_to_path["c8_superseded_ancestor_2"] = second
    key_to_path["c8_active_head"] = third
    key_to_path["t8_unchained_active_note"] = _compiled_note(
        root,
        title="Support rota current",
        slug="support-rota-current",
        observation="The current support rota has no prior revisions.",
        category="current state",
        tags=("support",),
    )

    # Distractor evidence/transcript pages padding C9's neighbourhood (N2):
    # composed from a generic outdoor-cooking word bank adjacent to, but
    # never reproducing, C2/C9's own gold pages or must_include facts.
    for index in range(distractor_count):
        _page(
            root,
            f"Knowledge Base/Evidence/context-activation/distractor-{index:04d}.md",
            f"Distractor evidence {index:04d}",
            _distractor_body(rng, index),
            extra_frontmatter="type: evidence\n",
        )

    leaks = (
        find_verbatim_leaks(root)
        + find_normalized_leaks(root)
        + find_fact_leaks_outside_gold_poison_pages(root, key_to_path)
    )
    if leaks:
        raise FixtureError(f"fixture turn(s) or fact(s) leaked (verbatim or near-verbatim) into the corpus: {leaks!r}")

    sorted_paths = dict(sorted(key_to_path.items()))
    return CorpusManifest(
        corpus_id=CORPUS_ID,
        seed=seed,
        distractor_count=distractor_count,
        key_to_path=sorted_paths,
        corpus_hash=_corpus_hash(root),
        logical_hash=_logical_corpus_hash(
            root,
            seed=seed,
            distractor_count=distractor_count,
            key_to_path=sorted_paths,
        ),
        state_sources=STATE_SOURCE_DECLARATIONS,
    )


def _run_isolated_build_request(request_path: str, result_path: str) -> None:
    """Child-process entry point for one isolated canonical corpus build."""

    request = json.loads(Path(request_path).read_text(encoding="utf-8"))
    manifest = _build_corpus_in_process(
        Path(request["root"]),
        seed=int(request["seed"]),
        distractor_count=int(request["distractor_count"]),
    )
    payload = {
        "corpus_id": manifest.corpus_id,
        "seed": manifest.seed,
        "distractor_count": manifest.distractor_count,
        "key_to_path": manifest.key_to_path,
        "corpus_hash": manifest.corpus_hash,
        "logical_hash": manifest.logical_hash,
        "state_sources": [asdict(item) for item in manifest.state_sources],
    }
    Path(result_path).write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def build_corpus(
    root: Path, *, seed: int = 20260916, distractor_count: int = DEFAULT_DISTRACTOR_COUNT
) -> CorpusManifest:
    """Build the corpus in a child with private state, leases, config, and logs.

    The caller's environment and already-created Exomem singletons are never
    rebound. Any canonical writer refusal fails the child and therefore the
    public build instead of falling back to hand-authored fixture bytes.
    """

    root = Path(root).resolve()
    repository = Path(__file__).resolve().parents[3]
    with tempfile.TemporaryDirectory(prefix="exomem-context-activation-") as runtime_raw:
        runtime = Path(runtime_raw)
        paths = {
            "state": runtime / "state",
            "xdg_state": runtime / "xdg-state",
            "config": runtime / "config.json",
            "logs": runtime / "logs",
            "call_ledger": runtime / "call-ledger",
            "writer_lease": runtime / "writer-lease",
            "lease_db": runtime / "lease-coordinator.sqlite",
            "tmp": runtime / "tmp",
            "request": runtime / "request.json",
            "result": runtime / "result.json",
        }
        for key in ("state", "xdg_state", "logs", "call_ledger", "writer_lease", "tmp"):
            paths[key].mkdir(parents=True, exist_ok=True)
        paths["request"].write_text(
            json.dumps(
                {"root": str(root), "seed": seed, "distractor_count": distractor_count},
                sort_keys=True,
            ),
            encoding="utf-8",
        )

        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("EXOMEM_")
        }
        inherited_pythonpath = environment.get("PYTHONPATH", "")
        environment.update(
            {
                "PYTHONPATH": os.pathsep.join(
                    path
                    for path in (
                        str(repository / "src"),
                        str(repository / "benchmarks"),
                        inherited_pythonpath,
                    )
                    if path
                ),
                "EXOMEM_STATE_ROOT": str(paths["state"]),
                "EXOMEM_CONFIG_PATH": str(paths["config"]),
                "EXOMEM_LOG_DIR": str(paths["logs"]),
                "EXOMEM_CALL_LEDGER_DIR": str(paths["call_ledger"]),
                "EXOMEM_WRITER_LEASE_STATE_DIR": str(paths["writer_lease"]),
                "EXOMEM_LEASE_COORDINATOR_DB": str(paths["lease_db"]),
                "EXOMEM_VAULT_PATH": str(root),
                "EXOMEM_DISABLE_EMBEDDINGS": "1",
                "EXOMEM_DISABLE_GRAPH_DRAIN": "1",
                "EXOMEM_DISABLE_GRAPH_SCHEDULING": "1",
                "XDG_STATE_HOME": str(paths["xdg_state"]),
                "TMPDIR": str(paths["tmp"]),
            }
        )
        code = (
            "from epistemic.corpora.context_activation import _run_isolated_build_request; "
            f"_run_isolated_build_request({str(paths['request'])!r}, {str(paths['result'])!r})"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                timeout=180,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise FixtureError("isolated corpus build exceeded 180 seconds") from error
        if completed.returncode != 0:
            detail = completed.stderr[-4000:].strip() or completed.stdout[-4000:].strip()
            raise FixtureError(
                f"isolated corpus build failed with exit {completed.returncode}: {detail}"
            )
        if not paths["result"].is_file():
            raise FixtureError("isolated corpus build returned no manifest")
        payload = json.loads(paths["result"].read_text(encoding="utf-8"))
        payload["state_sources"] = tuple(
            StateSourceDeclaration(**item) for item in payload["state_sources"]
        )
        return CorpusManifest(**payload)


def parse_records_items(page_text: str) -> list[dict[str, str]]:
    """Parse a records-collection page's frontmatter ``items`` list.

    Deliberately independent of any real YAML parser (this repository's
    corpus generator writes a small, fixed subset of YAML by hand, and a
    full parser dependency would be disproportionate to reading four known
    lines): pulls each ``- observed_on: "..."`` / ``  status: ...`` pair in
    frontmatter order. Returns them in file order; :func:`latest_record` is
    what actually determines "latest" -- by ``observed_on``, never by which
    line happens to come first.
    """

    items: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in page_text.splitlines():
        stripped = line.strip()
        match_date = re.match(r'-\s*observed_on:\s*"?([^"\s]+)"?', stripped)
        if match_date:
            if current:
                items.append(current)
            current = {"observed_on": match_date.group(1)}
            continue
        match_status = re.match(r"status:\s*(\S+)", stripped)
        if match_status and current:
            current["status"] = match_status.group(1)
    if current:
        items.append(current)
    return items


def latest_record(items: list[dict[str, str]]) -> dict[str, str] | None:
    """The item with the latest ``observed_on`` date, never the first in file order."""

    if not items:
        return None
    return max(items, key=lambda item: item["observed_on"])


# --------------------------------------------------------------------------
# The resolver-soundness probe corpus (``make-anchor-resolution-sound``,
# tasks 1.2/8): a dense hub cluster, a negative twin whose recall hits fall
# inside it (T10), a turn whose only link to a hub is a recall hit on the
# hub's OWN NEIGHBOUR rather than the hub itself (T11), and a one-word
# reference to a uniquely, qualifier-titled resource (C10).
#
# Deliberately NOT folded into ``FIXTURES``/``CASE_IDS``/``TWIN_IDS``: those
# tuples are shared with ``membench.utility.context_activation_arms``' own
# session-count and variant-generation math (`len(CASE_IDS)`, a hardcoded
# eighteen-packet run size), which this change's impact area
# (``working_set_resolve.py``, ``working_set_index.py``, ``working_set.py``,
# and this corpus/scorer pair) never names. Extending those shared tuples
# would ripple into that sibling module's own pre-registered numbers for no
# reason this change owns. This probe is scored directly against the real
# resolver (see ``tests/test_context_activation_resolver_soundness.py``),
# never through ``score_case``/``run_audit``, so it needs no digest, gold or
# poison list of its own -- only real anchor paths a test can assert on.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SoundnessProbeManifest:
    """Real vault-relative paths for the dense-cluster soundness probe."""

    hub_paths: tuple[str, ...]
    support_paths: tuple[str, ...]
    #: `hub_paths[0]`'s own typed-link neighbour that is NOT itself a hub --
    #: T11's turn only ever reaches this one page, never the hub itself.
    first_hub_own_neighbour: str
    #: A uniquely, qualifier-titled resource for the one-word-reference case.
    bike_path: str


#: T10: unrelated to every page in the corpus. T11: also unrelated -- its
#: only "link" to `hub_paths[0]` is a recall hit on that hub's own neighbour.
T10_TURN = "What's a good name for a new houseplant?"
T11_TURN = "Is it going to rain again this weekend?"
#: C10: a one-word reference to `bike_path`'s uniquely qualifier-titled page.
C10_TURN = "Is the bike still in the garage?"


def build_soundness_probe_corpus(root: Path, *, hub_count: int = 6, support_count: int = 14) -> SoundnessProbeManifest:
    """Render the dense-cluster probe under ``root`` (a directory of its own,
    never mixed into :func:`build_corpus`'s tree, so this probe's pages never
    become distractors or gold/poison pages of the pre-registered fixtures).

    ``hub_count`` hub anchors (``tags: [hub]``) ring-link their successor and
    each link a rotating window of four ``support_count`` plain notes (never
    anchors themselves) -- a hub or a person linking dozens of pages, the
    mechanism the real vault reproduction named. ``hub_count + support_count``
    is at least twenty pages with at least six anchors (design.md decision 7).
    """

    root = Path(root)
    # Under the governed KB folder (`kbdir.kb_dirname()`, default "Knowledge
    # Base"): the activation index only ever walks that subtree, and this
    # probe is built for the real index, not for `build_corpus`'s own
    # KB-prefix-free convention (never exercised through the index).
    support_paths = [
        _page(
            root,
            f"Knowledge Base/Notes/ClusterSupport/support-{i:02d}.md",
            f"Cluster support note {i:02d}",
            "Generic supporting material for the dense-cluster resolver-soundness probe.",
        )
        for i in range(1, support_count + 1)
    ]
    hub_paths: list[str] = []
    for i in range(1, hub_count + 1):
        next_hub_title = f"Cluster node {(i % hub_count) + 1:02d}"
        support_links = " ".join(f"[[Cluster support note {((i - 1 + o) % support_count) + 1:02d}]]" for o in range(4))
        body = f"A densely linked hub node in the resolver-soundness probe. See {support_links} and [[{next_hub_title}]]."
        hub_paths.append(
            _page(
                root,
                f"Knowledge Base/Hubs/cluster-node-{i:02d}.md",
                f"Cluster node {i:02d}",
                body,
                extra_frontmatter="tags: [hub]\n",
            )
        )
    bike_path = _page(
        root,
        "Knowledge Base/Products/spare-bike.md",
        "Bike (spare, blue frame)",
        "A spare bicycle kept for errands, distinct from the household's main one.",
    )
    return SoundnessProbeManifest(
        hub_paths=tuple(hub_paths),
        support_paths=tuple(support_paths),
        first_hub_own_neighbour=support_paths[0],
        bike_path=bike_path,
    )


__all__ = [
    "ALL_FACT_PHRASES",
    "ALL_TURNS",
    "BASE_DISTRACTOR_COUNT",
    "CASE_IDS",
    "CORPUS_ID",
    "DEFAULT_DISTRACTOR_COUNT",
    "EXPECTED_STATUSES",
    "FIXTURE_SET_ID",
    "FIXTURES",
    "GOLD_POISON_FACTS",
    "KEY_KINDS",
    "MEASURED_LATENCY_DATE",
    "MEASURED_LATENCY_MS",
    "MEASURED_LATENCY_SAMPLE_SIZE",
    "NARROW_GOLD_TWIN_IDS",
    "TWIN_IDS",
    "C10_TURN",
    "T10_TURN",
    "T11_TURN",
    "CorpusManifest",
    "FixtureCase",
    "FixtureError",
    "ProjectionBinding",
    "ReferenceBinding",
    "STATE_SOURCE_DECLARATIONS",
    "SoundnessProbeManifest",
    "StateSourceDeclaration",
    "anchor_kind_for",
    "assert_manifest_consistent",
    "build_corpus",
    "build_soundness_probe_corpus",
    "cases",
    "fact_for",
    "find_fact_leaks_outside_gold_poison_pages",
    "find_normalized_leaks",
    "find_verbatim_leaks",
    "fixture_by_id",
    "fixture_set_digest",
    "freeze_reference_binding",
    "latest_record",
    "parse_records_items",
    "reference_binding_digests",
    "twins",
]
