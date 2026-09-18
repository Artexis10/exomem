"""Agent-in-the-loop arms for the context-activation variants (task 3).

Rides the released ``f32 utility_action_episode`` family as new variants (a
code-tuple extension, no new amendment/receipt) per ``openspec/changes/
add-context-activation-benchmark``. Reuses ``epistemic.journeys.f27_replay``'s
isolation primitives -- envelope discovery, the environment floor, per-turn
argv construction, the unsafe-``--out`` refusal -- directly rather than
duplicating them. It deliberately does **not** reuse ``f27_replay.run_journey``
/ ``ARMS`` / ``_arm_plan`` themselves: those are wired to the two-arm
(hookless/hooked) *lifecycle-replay* corpus and its multi-turn vault-seeding
shape. Context-activation's five arms differ in which Exomem surface is
available for one cold-start turn, not in plugin-vs-hookless client shape, so
this module supplies its own thin, single-turn analogue of ``_arm_plan`` /
``_dry_run_lines`` built from the same low-level primitives.

Nothing in this module executes an agent. Building an argv and printing it
(``dry_run_lines``) is the only supported entry point; a real ``claude -p``
replay is a separate, explicitly authorized, opt-in paid probe per the
existing paid-probe rule (``epistemic-utility-regression``, "Paid probes are
bounded and opt-in").
"""

from __future__ import annotations

import dataclasses
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from epistemic.corpora.context_activation import (
    CASE_IDS,
    FIXTURES,
    GOLD_POISON_FACTS,
    TWIN_IDS,
    FixtureCase,
    fixture_by_id,
)
from epistemic.journeys.f27_replay import (
    AgentEnvelope,
    Arm,
    arm_environment,
    build_turn_argv,
    environment_delta,
    environment_floor,
    refuse_unsafe_out_dir,
)

from membench.ids import stable_id
from membench.utility.schema import CONTEXT_ACTIVATION_VARIANTS

#: One arm per pre-registered comparison point (design.md D2/D6): control (no
#: Exomem), raw recall (``ask_memory`` available, no instruction), compiler
#: (one mandated ``activate_context`` call -- the tool ships under the
#: sibling ``add-context-activation`` change, not here), nudged recall (A2
#: plus a search-first instruction), oracle packet (a hand-written per-case
#: ceiling, instrument-only, never scored against the compiler as product
#: performance).
ARM_IDS: tuple[str, ...] = ("A1_control", "A2_raw_recall", "A3_compiler", "A4_nudged_recall", "A5_oracle_packet")

DEFAULT_MODEL = "sonnet"
#: One cold-start turn per episode: small headroom for a tool call, not a
#: multi-turn conversation.
DEFAULT_MAX_TURNS = 4

_A3_MANDATE_TEXT = (
    "Before answering, call the `activate_context` tool exactly once with this "
    "turn's exact text, then answer using what it returns.\n"
)
_A4_NUDGE_TEXT = (
    "Before answering, search memory (`ask_memory`) for anything relevant to "
    "this turn, then answer using what you find.\n"
)


class ArmSetupError(ValueError):
    """Raised for an unknown case id, variant, or arm id."""


def variant_for_case(case_id: str) -> str:
    if case_id not in (*CASE_IDS, *TWIN_IDS):
        raise ArmSetupError(f"unknown case id {case_id!r}")
    return f"context_activation_{case_id.lower()}"


def case_for_variant(variant: str) -> str:
    if variant not in CONTEXT_ACTIVATION_VARIANTS:
        raise ArmSetupError(f"unknown context-activation variant {variant!r}; expected one of {CONTEXT_ACTIVATION_VARIANTS}")
    return variant.removeprefix("context_activation_").upper()


def _assert_variants_match_fixtures() -> None:
    """Drift guard: the schema's literal tuple and the fixture case ids agree."""

    expected = {variant_for_case(fixture.case_id) for fixture in FIXTURES}
    if expected != set(CONTEXT_ACTIVATION_VARIANTS):
        raise ArmSetupError(
            "membench.utility.schema.CONTEXT_ACTIVATION_VARIANTS has drifted from "
            "epistemic.corpora.context_activation.FIXTURES"
        )


_assert_variants_match_fixtures()


#: Distinct multipliers for `rotate_arm_order`'s mix (minor fix): a plain
#: `(seed + case_index) % n` is symmetric, so (seed=1, case_index=2) and
#: (seed=2, case_index=1) rotate to the *same* offset even though they are
#: two different episodes -- not the independent rotation the design intends.
#: Weighting the two terms differently breaks that symmetry while staying a
#: pure, auditable function (no hash-collision risk across a small seed
#: range, unlike a cryptographic hash mod 5).
_SEED_WEIGHT = 7
_CASE_INDEX_WEIGHT = 13


def rotate_arm_order(seed: int, case_index: int) -> tuple[str, ...]:
    """Rotate :data:`ARM_IDS` by seed and case index (design.md D6: "rotated arm order").

    A pure rotation, not a shuffle: every arm runs exactly once per episode
    regardless of order, so a rotation is sufficient to counter any ordering
    effect while keeping the five arms' relative sequence auditable by hand.
    """

    offset = (seed * _SEED_WEIGHT + case_index * _CASE_INDEX_WEIGHT) % len(ARM_IDS)
    return ARM_IDS[offset:] + ARM_IDS[:offset]


@dataclass(frozen=True)
class ContextActivationEpisode:
    """A seeded episode binding one fixture case to five paired arms.

    Deliberately not ``membench.utility.schema.Episode``/``EpisodeOracle``:
    those are shaped for a project-configuration action world
    (``ProjectState``, ``target_project``...) with no analogue for a
    conversational cold-start turn. ``episode_id`` is what pairs the five
    arms on one identity; ``oracle`` (the fixture itself, gold/poison/
    must-include/exclude/reminder) is evaluator-only and is never handed to
    an actor -- see ``actor_turn_text``.
    """

    episode_id: str
    seed: int
    variant: str
    case_id: str
    arm_order: tuple[str, ...]
    oracle: FixtureCase


def generate_context_activation_episode(seed: int, variant: str) -> ContextActivationEpisode:
    """Build one seeded episode binding a fixture case to a seeded arm order.

    Deterministic in ``(seed, variant)``, matching ``scenarios.generate_episode``'s
    own contract for the original three variants.
    """

    case_id = case_for_variant(variant)
    fixture = fixture_by_id(case_id)
    episode_id = stable_id("EPI", str(seed), variant)
    arm_order = rotate_arm_order(seed, CONTEXT_ACTIVATION_VARIANTS.index(variant))
    return ContextActivationEpisode(
        episode_id=episode_id, seed=seed, variant=variant, case_id=case_id, arm_order=arm_order, oracle=fixture
    )


def actor_turn_text(episode: ContextActivationEpisode) -> str:
    """The single actor-visible turn for an episode: the cold-start turn only.

    Never the reminder turn, gold, poison, must-include/exclude or expected
    status -- those are evaluator-only, exactly like ``scenarios.actor_view``
    excludes ``Episode.oracle``/``seed`` from what an actor may see.
    """

    return episode.oracle.turn


def oracle_packet_text(fixture: FixtureCase) -> str:
    """A5's hand-written-per-case packet, rendered as injectable text.

    B6: reads the fixture's own ``oracle_text`` field -- authored
    independently of ``must_include`` -- rather than deriving text from
    ``must_include`` itself. Deriving A5's ceiling packet from the same facts
    the reminder-turn test checks would make A5 pass that test by
    construction, not because it is a plausible packet; ``oracle_text`` is a
    separate, hand-written estimate of what a correct packet would actually
    say. Empty for C6 (the no-memory case, whose oracle is the abstained
    packet by design) and for every twin (each twin's oracle is abstained by
    design): A5 gets nothing to hand over because there is nothing correct to
    hand over.
    """

    return fixture.oracle_text


def strike_rule_applies(fixture: FixtureCase) -> bool:
    """Whether a case is in scope for the reminder-turn "strike" pass/fail at all.

    Only a case with something to resolve or narrow down -- ``resolved`` or
    ``ambiguous`` expected status -- has a fact the reminder turn could add;
    an ``unresolved``/no-memory or merely ``partial`` case has no positive
    claim for the first response to have gotten right or wrong.
    """

    return fixture.expected_status in ("resolved", "ambiguous")


def build_context_activation_arm(arm_id: str) -> Arm:
    """One arm's client-surface configuration, reusing ``f27_replay.Arm``.

    Every arm carries no plugin and no ``f27``-documented custom-instructions
    block (``uses_custom_instructions=False``): A3/A4/A5's system-prompt
    text is supplied directly to :func:`context_activation_turn_argv` via
    ``build_turn_argv``'s own ``append_system_prompt_file`` parameter, not
    through ``f27_replay.custom_instructions_block`` (which reads a fixed,
    unrelated, prominence-level block from the hookless-arm's own doc).
    """

    if arm_id not in ARM_IDS:
        raise ArmSetupError(f"unknown arm id {arm_id!r}; expected one of {ARM_IDS}")
    allowed_tools = {
        "A1_control": "",
        "A2_raw_recall": "mcp__exomem",
        "A3_compiler": "mcp__exomem",
        "A4_nudged_recall": "mcp__exomem",
        "A5_oracle_packet": "",
    }[arm_id]
    # A3/A4/A5 each carry a system-prompt-append file (the mandate, the
    # nudge, or the oracle packet); `build_turn_argv` requires this flag to
    # agree with whether a file is actually supplied per call, independent
    # of `f27_replay.custom_instructions_block`'s own (unrelated) content.
    uses_custom_instructions = arm_id in ("A3_compiler", "A4_nudged_recall", "A5_oracle_packet")
    return Arm(
        arm_id=arm_id,
        prominence="n/a",
        tools="",
        allowed_tools=allowed_tools,
        uses_plugin=False,
        uses_custom_instructions=uses_custom_instructions,
        configure_ref=f"context-activation-{arm_id.lower()}",
    )


def system_prompt_text_for(arm_id: str, episode: ContextActivationEpisode) -> str | None:
    """The system-prompt-append text an arm needs, or ``None`` for A1/A2."""

    if arm_id == "A3_compiler":
        return _A3_MANDATE_TEXT
    if arm_id == "A4_nudged_recall":
        return _A4_NUDGE_TEXT
    if arm_id == "A5_oracle_packet":
        return oracle_packet_text(episode.oracle) or None
    return None


@dataclass(frozen=True)
class ArmTurnPlan:
    """Everything one arm's one-turn invocation needs, and everything a test needs to fake it."""

    arm_id: str
    episode_id: str
    workdir: Path
    env: dict[str, str]
    argv: list[str]
    system_prompt_file: Path | None


def context_activation_turn_argv(
    *,
    episode: ContextActivationEpisode,
    arm_id: str,
    envelope: AgentEnvelope,
    out_dir: Path,
    model: str = DEFAULT_MODEL,
    max_turns: int = DEFAULT_MAX_TURNS,
    parent_env: Mapping[str, str] | None = None,
) -> ArmTurnPlan:
    """One arm's exact single-turn invocation for one episode.

    Built entirely from ``f27_replay.py``'s own primitives
    (:func:`environment_floor`, :func:`arm_environment`, :func:`build_turn_argv`,
    :func:`refuse_unsafe_out_dir`). Writes nothing and executes nothing: the
    system-prompt file's *path* is computed and returned, never created, so
    calling this function has zero side effects, matching ``run_journey``'s
    own ``dry_run=True`` contract.
    """

    out_dir = refuse_unsafe_out_dir(out_dir)
    workdir = out_dir / episode.case_id / arm_id
    mcp_config = workdir / "mcp.json"

    prompt_text = system_prompt_text_for(arm_id, episode)
    system_prompt_file = workdir / "system-prompt.txt" if prompt_text else None
    # `uses_custom_instructions` must agree with whether a file is actually
    # supplied for *this* episode, not just this arm_id: A5's oracle packet
    # is empty for a case with no `must_include` facts (e.g. C6), so A5
    # carries no file there even though it does for every other case.
    arm = dataclasses.replace(build_context_activation_arm(arm_id), uses_custom_instructions=system_prompt_file is not None)

    parent = dict(os.environ if parent_env is None else parent_env)
    floor, _removed = environment_floor(parent)
    if arm_id == "A1_control":
        # M6: A1 must have Exomem truly *absent*, not merely denied via the
        # tool allowlist over an otherwise-normal isolated environment.
        # `arm_environment` unconditionally injects `EXOMEM_VAULT_PATH` /
        # `EXOMEM_CONFIG_PATH` / etc. for every arm (even one with no
        # allowed tools) so that a hooked plugin arm's child processes never
        # see a stray real-vault variable; A1 carries no plugin and no
        # allowed tools at all, so it gets the floor with every `EXOMEM_*`
        # key stripped and nothing re-added -- the same shape a session with
        # no Exomem installed would present.
        env = {key: value for key, value in floor.items() if not key.startswith("EXOMEM_")}
    else:
        env = arm_environment(floor, arm=arm, workdir=workdir, vault=workdir / "vault")

    argv = build_turn_argv(
        executable=envelope.executable,
        arm=arm,
        prompt=actor_turn_text(episode),
        mcp_config=mcp_config,
        session_id=episode.episode_id,
        model=model,
        max_turns=max_turns,
        first=True,
        append_system_prompt_file=system_prompt_file,
        plugin_dir=None,
    )
    return ArmTurnPlan(
        arm_id=arm_id, episode_id=episode.episode_id, workdir=workdir, env=env, argv=argv, system_prompt_file=system_prompt_file
    )


def dry_run_lines(
    plan: ArmTurnPlan, *, parent_env: Mapping[str, str] | None = None
) -> list[str]:
    """What ``plan`` would do, in full, having done none of it. Never executes anything."""

    import shlex

    parent = dict(os.environ if parent_env is None else parent_env)
    removed, added, changed = environment_delta(parent, plan.env)
    lines = [
        f"arm {plan.arm_id}  episode {plan.episode_id}",
        f"  cwd it would run from: {plan.workdir}",
        f"  env removed: {', '.join(removed) or 'nothing'}",
        f"  env added:   {', '.join(added) or 'nothing'}",
        f"  env changed: {', '.join(changed) or 'nothing'}",
    ]
    if plan.system_prompt_file is not None:
        lines.append(f"  system-prompt file it would write: {plan.system_prompt_file}")
    lines.append(f"  argv: {shlex.join(plan.argv)}")
    return lines


def estimate_session_count(*, case_ids: Sequence[str], arm_ids: Sequence[str], repeats: int) -> int:
    """Sessions = cases x arms x repeats. A pure count, never a cost figure."""

    return len(case_ids) * len(arm_ids) * repeats


class BudgetError(ValueError):
    """Raised when a planned run's session count would exceed the cost cap."""


#: The paid-probe ceiling for one full run of this benchmark (M1): a bound
#: pre-registered here, not discovered mid-run. Mirrors the existing
#: `epistemic-utility-regression` paid-probe rule ("Paid probes are bounded
#: and opt-in") for this benchmark's own five-arm, cold-start-turn shape.
COST_CAP_USD = 25.0
#: Conservative per-episode reservation (one cold-start turn, `sonnet`,
#: `DEFAULT_MAX_TURNS` headroom for at most one tool call): a deliberate
#: overestimate, so `reserve_budget` refuses *before* a run starts spending
#: rather than after it has already overrun.
PER_EPISODE_RESERVATION_USD = 0.05


def reserve_budget(
    session_count: int, *, cap_usd: float = COST_CAP_USD, reservation_usd: float = PER_EPISODE_RESERVATION_USD
) -> float:
    """Reserve budget for ``session_count`` sessions, or refuse the run outright.

    Returns the reserved total (never a running/actual spend figure -- this
    module executes nothing, so it has no way to observe real spend). Raises
    :class:`BudgetError` when the reservation alone would already exceed the
    cap, which is meant to catch a mis-sized run (e.g. an accidental
    ``repeats=50``) before a single session is attempted, not after.
    """

    reserved = session_count * reservation_usd
    if reserved > cap_usd:
        raise BudgetError(
            f"{session_count} sessions at ${reservation_usd:.4f} each would reserve "
            f"${reserved:.2f}, over the ${cap_usd:.2f} cap"
        )
    return reserved


# --------------------------------------------------------------------------
# Task 3.3: reminder-turn test, blind extraction + model-free intersection,
# blind rubric input. All pure, deterministic, and grader-agnostic: the
# "grader" that lists asserted/requested facts is an injected callable, so
# this module has no model dependency of its own (the intersection with
# gold/poison is model-free by construction; only the extraction step, once
# a real run exists, would be backed by a model -- never here).
# --------------------------------------------------------------------------


class BlindExtractor:
    """The shape a grader callable must satisfy. Never handed gold/poison."""

    def __call__(self, turn: str, response: str) -> ExtractedFacts:  # pragma: no cover - protocol
        raise NotImplementedError


@dataclass(frozen=True)
class ExtractedFacts:
    """A grader's output: what it read as asserted and what it read as still requested."""

    asserted: tuple[str, ...]
    requested: tuple[str, ...]


@dataclass(frozen=True)
class ReminderTurnResult:
    """The primary agent-layer score (design.md D4): whether the first
    response already reflected the fact the reminder turn would have
    supplied -- deterministic, no grader involved in the pass/fail itself."""

    case_id: str
    reflected: bool
    matched_gold: tuple[str, ...]
    missing_gold: tuple[str, ...]


def score_reminder_turn(response_text: str, fixture: FixtureCase) -> ReminderTurnResult:
    """Whether ``response_text`` already reflects the fixture's required facts.

    Model-free: a literal substring check against ``fixture.must_include``
    (authored independently of A5's ``oracle_text``; see B6 in
    ``membench.utility.context_activation_arms``'s module history). A case
    that declares no ``must_include`` facts is vacuously reflected -- there
    is nothing the reminder turn could have corrected -- though every
    pre-registered case now authors at least one (B4).
    """

    missing = tuple(fact for fact in fixture.must_include if fact not in response_text)
    matched = tuple(fact for fact in fixture.must_include if fact in response_text)
    return ReminderTurnResult(case_id=fixture.case_id, reflected=not missing, matched_gold=matched, missing_gold=missing)


@dataclass(frozen=True)
class BlindIntersection:
    """A grader's asserted/requested facts, intersected model-free with gold/poison."""

    case_id: str
    asserted_gold: tuple[str, ...]
    asserted_poison: tuple[str, ...]
    requested_gold: tuple[str, ...]
    requested_poison: tuple[str, ...]


def _normalize_fact_phrase(text: str) -> str:
    """Casefold, strip punctuation, and lightly stem so a grader's paraphrase
    ("the AI subscriptions collection page") matches the fixture's own
    authored fact phrase without requiring verbatim agreement (B5): a
    realistic grader reports facts in its own words, never a fixture's
    internal logical keys.
    """

    text = text.casefold()
    text = re.sub(r"[^\w\s]", "", text)
    words = [re.sub(r"(ing|ed|s)$", "", word) if len(word) > 4 else word for word in text.split()]
    return " ".join(words)


def _fact_phrase_matches(extracted_item: str, fact_phrase: str) -> bool:
    """Whether ``extracted_item`` plausibly names the same fact as ``fact_phrase``.

    A majority-content-word overlap after normalisation, not exact equality
    or raw substring containment: a paraphrase need not repeat every word of
    the authored phrase, but a coincidental one- or two-word overlap should
    not count as the same fact either.
    """

    fact_words = set(_normalize_fact_phrase(fact_phrase).split())
    if not fact_words:
        return False
    item_words = set(_normalize_fact_phrase(extracted_item).split())
    overlap = fact_words & item_words
    return len(overlap) / len(fact_words) >= 0.6


def intersect_with_gold_poison(extraction: ExtractedFacts, fixture: FixtureCase) -> BlindIntersection:
    """Intersect a blind grader's extracted facts with gold/poison.

    The grader (``extraction``) is built from only the turn and the response
    (spec: "A grader that sees only the turn and the response SHALL list
    asserted facts and requested facts"); this function is the separate,
    model-free step that applies gold/poison *afterwards*, so the grader
    itself never sees them. B5: the grader's output is realistic prose (a
    paraphrase of what it read), so this intersects on the fixture's own
    ``GOLD_POISON_FACTS`` phrase per key -- normalised, majority-word-overlap
    matched -- rather than on raw key identity, which a real grader would
    never emit.
    """

    gold_facts = {key: GOLD_POISON_FACTS[key] for key in fixture.gold if key in GOLD_POISON_FACTS}
    poison_facts = {key: GOLD_POISON_FACTS[key] for key in fixture.poison if key in GOLD_POISON_FACTS}

    def _matched(items: tuple[str, ...], facts_by_key: dict[str, str]) -> tuple[str, ...]:
        return tuple(item for item in items if any(_fact_phrase_matches(item, phrase) for phrase in facts_by_key.values()))

    return BlindIntersection(
        case_id=fixture.case_id,
        asserted_gold=_matched(extraction.asserted, gold_facts),
        asserted_poison=_matched(extraction.asserted, poison_facts),
        requested_gold=_matched(extraction.requested, gold_facts),
        requested_poison=_matched(extraction.requested, poison_facts),
    )


def blind_rubric_input(fixture: FixtureCase, response_text: str) -> dict[str, str]:
    """The exact, minimal input a blind per-case usefulness rubric may see.

    Turn and response only -- no arm label, no gold, no poison, no expected
    status, matching the spec's "Grader never sees gold" scenario. The
    rubric's own 0/1/2 anchors are written by a human against
    ``fixture.case_id`` before any run and are not this function's concern.
    """

    return {"case_id": fixture.case_id, "turn": fixture.turn, "response": response_text}


def harness_fault_status(*, exit_code: int, is_error: bool, malformed_transcript: bool) -> str:
    """Map an f27-style harness fault into "blocked" -- never a scored loss.

    Mirrors ``f27_replay.fault_reason``'s own criteria (non-zero exit, an
    ``is_error``/error-subtype result, a malformed transcript line) without
    importing that function directly: it inspects a live subprocess/
    transcript object this module never constructs, whereas every arm here
    is dry-run only.
    """

    return "blocked" if (exit_code != 0 or is_error or malformed_transcript) else "not_blocked"


__all__ = [
    "ARM_IDS",
    "COST_CAP_USD",
    "DEFAULT_MAX_TURNS",
    "DEFAULT_MODEL",
    "PER_EPISODE_RESERVATION_USD",
    "ArmSetupError",
    "ArmTurnPlan",
    "BlindExtractor",
    "BlindIntersection",
    "BudgetError",
    "ContextActivationEpisode",
    "ExtractedFacts",
    "ReminderTurnResult",
    "actor_turn_text",
    "blind_rubric_input",
    "build_context_activation_arm",
    "case_for_variant",
    "context_activation_turn_argv",
    "dry_run_lines",
    "estimate_session_count",
    "generate_context_activation_episode",
    "harness_fault_status",
    "intersect_with_gold_poison",
    "oracle_packet_text",
    "reserve_budget",
    "rotate_arm_order",
    "score_reminder_turn",
    "strike_rule_applies",
    "system_prompt_text_for",
    "variant_for_case",
]
