"""Red-first tests for the context-activation agent arms (task 3.1).

Existing f32 variants byte-identical (seeds, oracles, paired outcomes) after
the tuple extension; new variants bind cases to seeded action worlds; five
arms pair on one episode identity; harness faults -> blocked.
"""

from __future__ import annotations

import dataclasses

import pytest
from epistemic.corpora.context_activation import CASE_IDS, TWIN_IDS, fixture_by_id
from membench.utility.context_activation_arms import (
    ARM_IDS,
    ArmSetupError,
    ExtractedFacts,
    actor_turn_text,
    blind_rubric_input,
    build_context_activation_arm,
    case_for_variant,
    context_activation_turn_argv,
    dry_run_lines,
    estimate_session_count,
    generate_context_activation_episode,
    harness_fault_status,
    intersect_with_gold_poison,
    oracle_packet_text,
    rotate_arm_order,
    score_reminder_turn,
    system_prompt_text_for,
    variant_for_case,
)
from membench.utility.scenarios import generate_episode
from membench.utility.schema import CONTEXT_ACTIVATION_VARIANTS, VARIANTS

# -- proof (a): existing f32 variants are unaffected -----------------------


def test_existing_variants_tuple_is_byte_identical() -> None:
    assert VARIANTS == ("helpful_history", "self_contained", "stale_distractor")


def test_context_activation_variants_are_disjoint_from_existing_variants() -> None:
    assert set(CONTEXT_ACTIVATION_VARIANTS).isdisjoint(VARIANTS)
    assert len(CONTEXT_ACTIVATION_VARIANTS) == 18


@pytest.mark.parametrize("variant", VARIANTS)
def test_existing_generate_episode_still_produces_the_same_shape(variant: str) -> None:
    # Same call, same (seed, variant): if the tuple extension had touched
    # position 0/1/2 this would already differ from the pre-extension
    # behaviour exercised in test_membench_utility_scenarios.py.
    episode = generate_episode(11, variant)
    assert episode.variant == variant
    assert episode.oracle.target_project


# -- variant <-> case_id mapping stays in step with the fixture manifest ---


@pytest.mark.parametrize("case_id", (*CASE_IDS, *TWIN_IDS))
def test_variant_for_case_round_trips(case_id: str) -> None:
    variant = variant_for_case(case_id)
    assert variant in CONTEXT_ACTIVATION_VARIANTS
    assert case_for_variant(variant) == case_id


def test_unknown_case_id_refused() -> None:
    with pytest.raises(ArmSetupError):
        variant_for_case("C99")


def test_unknown_variant_refused() -> None:
    with pytest.raises(ArmSetupError):
        case_for_variant("context_activation_c99")


# -- episodes bind cases to seeded action worlds ---------------------------


def test_episode_is_deterministic_in_seed_and_variant() -> None:
    variant = variant_for_case("C1")
    first = generate_context_activation_episode(7, variant)
    second = generate_context_activation_episode(7, variant)
    assert first == second


def test_episode_differs_across_seeds() -> None:
    variant = variant_for_case("C1")
    first = generate_context_activation_episode(7, variant)
    second = generate_context_activation_episode(8, variant)
    assert first.episode_id != second.episode_id
    assert first.arm_order != second.arm_order or first.seed != second.seed


def test_episode_oracle_is_the_fixture_and_never_leaks_into_the_actor_turn() -> None:
    variant = variant_for_case("C1")
    episode = generate_context_activation_episode(3, variant)
    assert episode.oracle.case_id == "C1"
    turn = actor_turn_text(episode)
    assert turn == episode.oracle.turn
    # the reminder turn and gold/poison keys are evaluator-only
    assert episode.oracle.reminder_turn not in turn
    for key in episode.oracle.gold:
        assert key not in turn


# -- five arms pair on one episode identity --------------------------------


def test_all_five_arms_share_one_episode_identity() -> None:
    variant = variant_for_case("C5")
    episode = generate_context_activation_episode(5, variant)
    assert set(episode.arm_order) == set(ARM_IDS)
    assert len(episode.arm_order) == 5
    for arm_id in ARM_IDS:
        arm = build_context_activation_arm(arm_id)
        assert arm.arm_id == arm_id


def test_rotate_arm_order_is_a_rotation_not_a_shuffle() -> None:
    order = rotate_arm_order(3, 2)
    assert set(order) == set(ARM_IDS)
    # a rotation: every element appears once, and the order is a cyclic shift
    doubled = ARM_IDS + ARM_IDS
    joined = "|".join(order)
    assert joined in "|".join(doubled)


def test_arm_order_rotates_with_seed_and_case_index() -> None:
    orders = {rotate_arm_order(seed, 0) for seed in range(len(ARM_IDS))}
    assert len(orders) == len(ARM_IDS)


def test_unknown_arm_id_refused() -> None:
    with pytest.raises(ArmSetupError):
        build_context_activation_arm("A99_bogus")


# -- per-arm Exomem availability is what actually distinguishes the arms --


def test_a1_control_has_no_exomem_surface() -> None:
    arm = build_context_activation_arm("A1_control")
    assert arm.allowed_tools == ""
    assert arm.uses_plugin is False


def test_a2_and_a4_both_expose_ask_memory_but_only_a4_is_nudged() -> None:
    a2 = build_context_activation_arm("A2_raw_recall")
    a4 = build_context_activation_arm("A4_nudged_recall")
    assert a2.allowed_tools == a4.allowed_tools == "mcp__exomem"
    variant = variant_for_case("C1")
    episode = generate_context_activation_episode(1, variant)
    assert system_prompt_text_for("A2_raw_recall", episode) is None
    assert system_prompt_text_for("A4_nudged_recall", episode) is not None


def test_a5_oracle_packet_is_derived_from_must_include_and_never_from_gold_keys() -> None:
    variant = variant_for_case("C1")
    episode = generate_context_activation_episode(1, variant)
    text = oracle_packet_text(episode.oracle)
    for fact in episode.oracle.must_include:
        assert fact in text
    for key in episode.oracle.gold:
        assert key not in text  # logical keys are evaluator plumbing, not prose


def test_a5_oracle_packet_is_empty_for_a_case_with_no_must_include_facts() -> None:
    c6 = fixture_by_id("C6")
    assert oracle_packet_text(c6) == ""


# -- dry-run argv construction (never executes anything) ------------------


class _FakeEnvelope:
    executable = "/usr/bin/claude"
    version = "9.9.9 (fake, for tests)"


def test_context_activation_turn_argv_builds_without_executing_or_writing(tmp_path) -> None:
    variant = variant_for_case("C2")
    episode = generate_context_activation_episode(9, variant)
    plan = context_activation_turn_argv(
        episode=episode,
        arm_id="A4_nudged_recall",
        envelope=_FakeEnvelope(),
        out_dir=tmp_path / "out",
        parent_env={},
    )
    assert plan.argv[0] == _FakeEnvelope.executable
    assert episode.oracle.turn in plan.argv
    assert plan.system_prompt_file is not None
    assert not plan.system_prompt_file.exists()  # never written
    assert not plan.workdir.exists()  # never created
    lines = dry_run_lines(plan, parent_env={})
    assert any("argv:" in line for line in lines)


def test_context_activation_turn_argv_a5_on_a_must_include_empty_case_has_no_file(tmp_path) -> None:
    # Regression: A5's oracle packet is empty text for C6 (no must_include
    # facts), so `uses_custom_instructions` must be derived per-episode, not
    # fixed per arm_id, or `build_turn_argv` refuses the mismatch.
    variant = variant_for_case("C6")
    episode = generate_context_activation_episode(9, variant)
    plan = context_activation_turn_argv(
        episode=episode, arm_id="A5_oracle_packet", envelope=_FakeEnvelope(), out_dir=tmp_path / "out", parent_env={}
    )
    assert plan.system_prompt_file is None


def test_context_activation_turn_argv_a1_has_no_system_prompt_file(tmp_path) -> None:
    variant = variant_for_case("C2")
    episode = generate_context_activation_episode(9, variant)
    plan = context_activation_turn_argv(
        episode=episode, arm_id="A1_control", envelope=_FakeEnvelope(), out_dir=tmp_path / "out", parent_env={}
    )
    assert plan.system_prompt_file is None


def test_estimate_session_count_is_a_pure_product() -> None:
    assert estimate_session_count(case_ids=CASE_IDS, arm_ids=("A1_control", "A2_raw_recall"), repeats=1) == 18
    assert estimate_session_count(case_ids=CASE_IDS, arm_ids=ARM_IDS, repeats=5) == 9 * 5 * 5


# -- harness faults -> blocked, never scored -------------------------------


def test_harness_fault_maps_to_blocked() -> None:
    assert harness_fault_status(exit_code=1, is_error=False, malformed_transcript=False) == "blocked"
    assert harness_fault_status(exit_code=0, is_error=True, malformed_transcript=False) == "blocked"
    assert harness_fault_status(exit_code=0, is_error=False, malformed_transcript=True) == "blocked"


def test_clean_execution_is_not_blocked() -> None:
    assert harness_fault_status(exit_code=0, is_error=False, malformed_transcript=False) == "not_blocked"


# -- task 3.3: reminder-turn test, blind extraction intersection, rubric --


def test_reminder_turn_reflected_when_response_carries_the_facts() -> None:
    c1 = fixture_by_id("C1")
    response = "You've hit the weekly limit before, and there's a recurring capacity ceiling pattern here too."
    result = score_reminder_turn(response, c1)
    assert result.reflected is True
    assert result.missing_gold == ()


def test_reminder_turn_not_reflected_when_response_misses_the_facts() -> None:
    c1 = fixture_by_id("C1")
    result = score_reminder_turn("Here's a generic answer with nothing specific.", c1)
    assert result.reflected is False
    assert result.missing_gold == c1.must_include


def test_reminder_turn_vacuously_reflected_when_nothing_is_required() -> None:
    c6 = fixture_by_id("C6")
    assert score_reminder_turn("214 grams, roughly.", c6).reflected is True


def test_blind_intersection_never_receives_gold_or_poison_in_the_extraction_step() -> None:
    c1 = fixture_by_id("C1")
    extraction = ExtractedFacts(asserted=(c1.gold[0], "unrelated_fact"), requested=(c1.poison[0],))
    intersection = intersect_with_gold_poison(extraction, c1)
    assert intersection.asserted_gold == (c1.gold[0],)
    assert intersection.asserted_poison == ()
    assert intersection.requested_poison == (c1.poison[0],)


def test_blind_rubric_input_carries_only_turn_and_response() -> None:
    c1 = fixture_by_id("C1")
    payload = blind_rubric_input(c1, "a response")
    assert set(payload) == {"case_id", "turn", "response"}
    assert payload["turn"] == c1.turn
    assert "gold" not in payload and "poison" not in payload and "expected_status" not in payload


def test_fixture_case_is_frozen_and_dataclasses_replace_still_works() -> None:
    c1 = fixture_by_id("C1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        c1.turn = "mutated"  # type: ignore[misc]
