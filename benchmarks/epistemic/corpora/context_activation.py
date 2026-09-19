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
import random
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

FIXTURE_SET_ID = "context-activation-fixtures-v1"
CORPUS_ID = "context-activation-corpus-v1"

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
class CorpusManifest:
    corpus_id: str
    seed: int
    distractor_count: int
    key_to_path: dict[str, str]
    corpus_hash: str


def _corpus_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.md")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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


def build_corpus(
    root: Path, *, seed: int = 20260916, distractor_count: int = DEFAULT_DISTRACTOR_COUNT
) -> CorpusManifest:
    """Render the seeded synthetic corpus under ``root``.

    Generated names only -- no personal names, products, hosts or
    vault-structure labels. Refuses (:class:`FixtureError`) if any fixture
    turn, reminder turn, or authored fact leaked verbatim or near-verbatim
    into a rendered page, which is exactly the contamination class
    ``design.md`` recorded against the real vault (2026-09-16 reproduction).
    """

    root = Path(root)
    rng = random.Random(seed)
    key_to_path: dict[str, str] = {}

    # C1/T1 -- AI-usage complaint vs. an unrelated fitness goal.
    key_to_path["c1_subscriptions_collection"] = _page(
        root,
        "Collections/ai-subscriptions.md",
        "AI subscriptions collection",
        "Tracks active AI tool subscriptions and their plan tiers.",
    )
    key_to_path["c1_weekly_limit_insight"] = _page(
        root,
        "Notes/weekly-limit-insight.md",
        "Weekly limit insight",
        "Usage tends to hit the weekly cap by Thursday on the current tier.",
    )
    key_to_path["c1_capacity_ceilings_pattern"] = _page(
        root,
        "Notes/capacity-ceilings-pattern.md",
        "Capacity ceilings pattern",
        "A recurring pattern: capacity ceilings are reached before renewal.",
    )
    key_to_path["t1_fitness_goal_note"] = _page(
        root,
        "Notes/fitness-goal-note.md",
        "Fitness goal note",
        "A step-count goal tracked for general fitness, unrelated to tooling.",
    )

    # C2/T2/C9/T9 -- planning to cook vs. photographing; C9/T9 reuse these
    # same pages, scored against the padded and unpadded trees respectively.
    key_to_path["c2_grill_equipment_page"] = _page(
        root,
        "Equipment/grill.md",
        "Grill equipment page",
        "A gas grill with a two-zone setup, serviced this spring.",
    )
    key_to_path["c2_cooking_method_insight"] = _page(
        root,
        "Notes/cooking-method-insight.md",
        "Cooking method insight",
        "Indirect heat works best for this recipe on the grill.",
    )
    key_to_path["t2_camera_gear_note"] = _page(
        root,
        "Equipment/camera.md",
        "Camera gear note",
        "A camera body and one prime lens kept for weekend photography.",
    )

    # C3/T3 -- a roadmap item and its design pointer, twinned across an
    # unrelated second project's own roadmap item.
    key_to_path["c3_planning_item"] = _page(
        root,
        "Planning/roadmap-item-a.md",
        "Roadmap item A",
        "Next step: extend the reporting module. See the design pointer.",
    )
    key_to_path["c3_design_pointer"] = _page(
        root,
        "Planning/roadmap-item-a-design.md",
        "Roadmap item A design pointer",
        "Design notes for roadmap item A live here.",
    )
    key_to_path["t3_other_project_planning_item"] = _page(
        root,
        "Planning/roadmap-item-b.md",
        "Roadmap item B (other workstream)",
        "Next step for the other workstream: migrate the billing job.",
    )

    # C4/T4 -- a named colleague and failure note vs. two entities sharing
    # one first name.
    key_to_path["c4_entity_profile"] = _page(
        root,
        "Entities/colleague-rowan.md",
        "Rowan Ashfield",
        "A colleague on the platform team.",
        extra_frontmatter="aliases: [Rowan]\n",
    )
    key_to_path["c4_failure_note"] = _page(
        root,
        "Notes/deployment-issue-rowan.md",
        "Deployment issue note",
        "Rowan Ashfield's deployment failed on the Mac build last month; root-caused to a config drift.",
    )
    key_to_path["t4_shared_first_name_entity_a"] = _page(
        root, "Entities/alex-monroe.md", "Alex Monroe", "A colleague on the data team."
    )
    key_to_path["t4_shared_first_name_entity_b"] = _page(
        root, "Entities/alex-park.md", "Alex Park", "A colleague on the support team."
    )

    # C5/T5 -- a resource made unavailable by its Records collection's
    # latest item (by date, never by line order -- N supplement), vs. an
    # available resource that must not be mislabeled.
    key_to_path["c5_resource_profile"] = _page(
        root,
        "Resources/workshop-bench.md",
        "Workshop bench",
        "A shared workshop bench, booked by session. See [[workshop-bench-records]] for status.",
    )
    key_to_path["c5_records_latest_unavailable"] = _page(
        root,
        "Resources/workshop-bench-records.md",
        "Workshop bench records",
        "Structured records collection for the workshop bench resource.",
        extra_frontmatter=(
            "semantic_profile: records\n"
            "fields: [observed_on, status]\n"
            "items:\n"
            '  - observed_on: "2026-08-02"\n'
            "    status: available\n"
            '  - observed_on: "2026-09-10"\n'
            "    status: unavailable\n"
        ),
    )
    key_to_path["t5_available_resource"] = _page(
        root,
        "Resources/scanner-cart.md",
        "Scanner cart",
        "A mobile scanner cart, currently available with no open issues.",
    )

    # C6/T6 -- no pages are seeded for the no-memory turn itself: it must
    # resolve nothing. T6 mentions the C2 grill only lexically.

    # C7/T7 -- an ambiguous domain triple with distinct, pairwise-disjoint
    # wikilink neighbourhoods (N supplement): three pages each hub links to,
    # shared by no other hub, so "disjoint neighbourhoods" is a real,
    # checkable property rather than an assertion about empty link sets.
    for suffix, blurb in (("a", "reference material"), ("b", "a design note"), ("c", "a status update")):
        _page(root, f"Hubs/feature-neighbour-{suffix}.md", f"Feature neighbour {suffix}", f"Feature-hub {blurb}.")
        _page(root, f"Hubs/market-neighbour-{suffix}.md", f"Market neighbour {suffix}", f"Market-hub {blurb}.")
        _page(root, f"Hubs/ux-neighbour-{suffix}.md", f"UX neighbour {suffix}", f"Search-UX-hub {blurb}.")
    key_to_path["c7_hub_feature"] = _page(
        root,
        "Hubs/ai-search-feature.md",
        "AI search feature hub",
        "Implementation hub for the in-app AI search feature. See "
        "[[feature-neighbour-a]], [[feature-neighbour-b]], [[feature-neighbour-c]].",
    )
    key_to_path["c7_hub_market"] = _page(
        root,
        "Hubs/ai-search-market.md",
        "AI search market hub",
        "Market-research hub comparing AI search engines. See "
        "[[market-neighbour-a]], [[market-neighbour-b]], [[market-neighbour-c]].",
    )
    key_to_path["c7_hub_search_ux"] = _page(
        root,
        "Hubs/search-ux.md",
        "Search UX hub",
        "UX research hub for search result presentation generally. See "
        "[[ux-neighbour-a]], [[ux-neighbour-b]], [[ux-neighbour-c]].",
    )

    # C8/T8 -- a supersession chain (two retired ancestors, one active
    # head), vs. a single note with no revision history at all.
    key_to_path["c8_superseded_ancestor_1"] = _page(
        root,
        "Notes/onboarding-approach-v1.md",
        "Onboarding approach v1",
        "The original onboarding approach, since retired.",
        status="superseded",
        superseded_by="[[onboarding-approach-v2]]",
    )
    key_to_path["c8_superseded_ancestor_2"] = _page(
        root,
        "Notes/onboarding-approach-v2.md",
        "Onboarding approach v2",
        "The second onboarding approach, since retired in turn.",
        status="superseded",
        superseded_by="[[onboarding-approach-v3]]",
    )
    key_to_path["c8_active_head"] = _page(
        root, "Notes/onboarding-approach-v3.md", "Onboarding approach v3", "The current onboarding approach."
    )
    key_to_path["t8_unchained_active_note"] = _page(
        root, "Notes/support-rota-current.md", "Support rota (current)", "The current support rota, with no prior revisions."
    )

    # Distractor evidence/transcript pages padding C9's neighbourhood (N2):
    # composed from a generic outdoor-cooking word bank adjacent to, but
    # never reproducing, C2/C9's own gold pages or must_include facts.
    for index in range(distractor_count):
        _page(root, f"Evidence/distractor-{index:04d}.md", f"Distractor evidence {index:04d}", _distractor_body(rng, index))

    leaks = (
        find_verbatim_leaks(root)
        + find_normalized_leaks(root)
        + find_fact_leaks_outside_gold_poison_pages(root, key_to_path)
    )
    if leaks:
        raise FixtureError(f"fixture turn(s) or fact(s) leaked (verbatim or near-verbatim) into the corpus: {leaks!r}")

    return CorpusManifest(
        corpus_id=CORPUS_ID,
        seed=seed,
        distractor_count=distractor_count,
        key_to_path=dict(sorted(key_to_path.items())),
        corpus_hash=_corpus_hash(root),
    )


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
    "SoundnessProbeManifest",
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
    "latest_record",
    "parse_records_items",
    "twins",
]
