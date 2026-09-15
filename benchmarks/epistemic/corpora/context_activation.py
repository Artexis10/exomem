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
resolve or stay ambiguous normally -- so the fixture asserts the absence of
one specific spurious signal (cross-case leakage, false ambiguity, a
fabricated unavailability label, a false supersession mark) rather than mere
silence. Deviation, flagged for the pre-registration owner: the spec's literal
scenario text ("any twin turn yields an anchor with status resolved -> the
case fails") is read here as "any anchor *outside a twin's own gold* resolves"
-- the strictly literal reading would make T3/T7/T8's own explicit design
narrative in ``design.md`` ("a scoped variant that resolves", "unchained
active note", "the other workstream's own item") unsatisfiable by
construction. See ``membench.utility.context_activation.score_case``.
"""

from __future__ import annotations

import hashlib
import json
import random
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
        must_include=("roadmap item",),
        must_exclude=(),
        expected_status="resolved",
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
        must_include=(),
        must_exclude=(),
        expected_status="resolved",
    ),
    FixtureCase(
        case_id="T4",
        pairs_with="C4",
        turn="Alex mentioned the deployment issue again.",
        reminder_turn="Which Alex — there are two on the team. Check before assuming either.",
        gold=(),
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
        turn="What's a good conversion for two cups of flour in grams?",
        reminder_turn="There's nothing to look up here — that's a general question, not something from our history.",
        gold=(),
        poison=(),
        roles=(),
        must_include=(),
        must_exclude=(),
        expected_status="unresolved",
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
        must_include=(),
        must_exclude=(),
        expected_status="ambiguous",
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
        must_include=("current",),
        must_exclude=("retired approach presented as current",),
        expected_status="resolved",
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
    # -- C9 / T9: C2 padded with ~200 distractor pages -----------------------
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
    ),
    FixtureCase(
        case_id="T9",
        pairs_with="C9",
        turn="I'm planning to paint this room this weekend.",
        reminder_turn="No connection to the cooking notes — this one's just painting.",
        gold=(),
        poison=("c2_grill_equipment_page", "c2_cooking_method_insight"),
        roles=(),
        must_include=(),
        must_exclude=(),
        expected_status="unresolved",
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


#: The naive-path latency constant (task 4.2), replacing the spec's
#: pre-registered p50 800 ms / p95 2,500 ms placeholder with a measured
#: figure. Measured 2026-09-16 on the personal cell (quiesced, checked via
#: /proc/<pid>/stat CPU deltas before the run): `ask_memory` over the 18
#: fixture turns, each with a nonce appended, `mode="hybrid"`, `limit=5`,
#: `detail="compact"`, `include_timings=true`; the figure is each call's
#: reported `timings.total_ms` (server-side, excludes MCP transport). n=18,
#: nearest-rank percentile (matching docs/benchmarks.md's own convention).
#: This constant carries only figures -- no vault path or title -- and is
#: the only part of the baseline committed to the repository. The full
#: per-case report (which does name real vault paths/titles, never body
#: content, per the private-instrument privacy rule) lives only in the
#: operator-passed output directory and is preserved as Evidence, never
#: committed here.
MEASURED_LATENCY_MS: dict[str, float] | None = {
    "n": 18,
    "min_ms": 881.653,
    "p50_ms": 10744.641,
    "p95_ms": 16488.201,
    "max_ms": 16823.787,
    "mean_ms": 11197.24,
    "measured_on": "2026-09-16",
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
    for case_id in CASE_IDS:
        case = by_id[case_id]
        if case.pairs_with is not None:
            raise FixtureError(f"{case_id}: a case must not carry pairs_with, got {case.pairs_with!r}")


#: Every non-empty turn and reminder turn in the fixture set, for the
#: corpus-contamination check (never checked in piecemeal: a leak in either
#: field is equally a leak).
ALL_TURNS: tuple[str, ...] = tuple(
    text for fixture in FIXTURES for text in (fixture.turn, fixture.reminder_turn) if text.strip()
)


def find_verbatim_leaks(
    root: Path, *, turns: Iterable[str] = ALL_TURNS
) -> tuple[tuple[str, str], ...]:
    """Every ``(relative page path, leaked turn)`` pair found verbatim under ``root``."""

    leaks: list[tuple[str, str]] = []
    for path in sorted(Path(root).rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(root).as_posix()
        for turn in turns:
            if turn and turn in text:
                leaks.append((rel, turn))
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


def build_corpus(
    root: Path, *, seed: int = 20260916, distractor_count: int = DEFAULT_DISTRACTOR_COUNT
) -> CorpusManifest:
    """Render the seeded synthetic corpus under ``root``.

    Generated names only -- no personal names, products, hosts or
    vault-structure labels. Refuses (:class:`FixtureError`) if any fixture
    turn or reminder turn leaked verbatim into a rendered page, which is
    exactly the contamination class ``design.md`` recorded against the real
    vault (2026-09-16 reproduction).
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

    # C2/T2/C9/T9 -- planning to cook vs. photographing; C9 reuses these
    # same pages inside the padded (distractor-heavy) corpus.
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
        "Rowan Ashfield flagged a deployment issue last month; root-caused to a config drift.",
    )
    key_to_path["t4_shared_first_name_entity_a"] = _page(
        root, "Entities/alex-monroe.md", "Alex Monroe", "A colleague on the data team."
    )
    key_to_path["t4_shared_first_name_entity_b"] = _page(
        root, "Entities/alex-park.md", "Alex Park", "A colleague on the support team."
    )

    # C5/T5 -- a resource made unavailable by its Records collection's
    # latest item, vs. an available resource that must not be mislabeled.
    key_to_path["c5_resource_profile"] = _page(
        root, "Resources/workshop-bench.md", "Workshop bench", "A shared workshop bench, booked by session."
    )
    key_to_path["c5_records_latest_unavailable"] = _page(
        root,
        "Resources/workshop-bench-records.md",
        "Workshop bench records",
        "manifest: records\n"
        "2026-09-10: bench marked unavailable for repair.\n"
        "2026-08-02: bench booked, returned in good order.",
    )
    key_to_path["t5_available_resource"] = _page(
        root,
        "Resources/scanner-cart.md",
        "Scanner cart",
        "A mobile scanner cart, currently available with no open issues.",
    )

    # C6/T6 -- no pages are seeded for the no-memory turn itself: it must
    # resolve nothing. T6 mentions the C2 grill only lexically.

    # C7/T7 -- an ambiguous domain triple with disjoint neighbourhoods.
    key_to_path["c7_hub_feature"] = _page(
        root, "Hubs/ai-search-feature.md", "AI search feature hub", "Implementation hub for the in-app AI search feature."
    )
    key_to_path["c7_hub_market"] = _page(
        root, "Hubs/ai-search-market.md", "AI search market hub", "Market-research hub comparing AI search engines."
    )
    key_to_path["c7_hub_search_ux"] = _page(
        root, "Hubs/search-ux.md", "Search UX hub", "UX research hub for search result presentation generally."
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

    # ~200 distractor evidence/transcript pages padding C9's neighbourhood.
    # Deterministic body text drawn only from a numeric RNG stream, so no
    # fixture vocabulary can leak in through this loop.
    for index in range(distractor_count):
        _page(
            root,
            f"Evidence/distractor-{index:04d}.md",
            f"Distractor evidence {index:04d}",
            f"Background synthetic transcript material, entry {rng.randint(0, 999999):06d}.",
        )

    leaks = find_verbatim_leaks(root)
    if leaks:
        raise FixtureError(f"fixture turn(s) leaked verbatim into the corpus: {leaks!r}")

    return CorpusManifest(
        corpus_id=CORPUS_ID,
        seed=seed,
        distractor_count=distractor_count,
        key_to_path=dict(sorted(key_to_path.items())),
        corpus_hash=_corpus_hash(root),
    )


__all__ = [
    "ALL_TURNS",
    "CASE_IDS",
    "CORPUS_ID",
    "DEFAULT_DISTRACTOR_COUNT",
    "EXPECTED_STATUSES",
    "FIXTURE_SET_ID",
    "FIXTURES",
    "KEY_KINDS",
    "MEASURED_LATENCY_MS",
    "TWIN_IDS",
    "CorpusManifest",
    "FixtureCase",
    "FixtureError",
    "anchor_kind_for",
    "assert_manifest_consistent",
    "build_corpus",
    "cases",
    "find_verbatim_leaks",
    "fixture_by_id",
    "fixture_set_digest",
    "twins",
]
