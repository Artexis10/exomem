"""Red-first tests for the context-activation agent arms (task 3.1).

Existing f32 variants byte-identical (seeds, oracles, paired outcomes) after
the tuple extension; new variants bind cases to seeded action worlds; five
arms pair on one episode identity; harness faults -> blocked.
"""

from __future__ import annotations

import dataclasses

import pytest
from epistemic.corpora.context_activation import (
    CASE_IDS,
    FIXTURES,
    GOLD_POISON_FACTS,
    TWIN_IDS,
    fixture_by_id,
)
from membench.utility.context_activation_arms import (
    ARM_IDS,
    COST_CAP_USD,
    MECHANISM_ACCEPT_BAR,
    PER_EPISODE_RESERVATION_USD,
    ArmSetupError,
    BudgetError,
    ExtractedFacts,
    actor_turn_text,
    blind_rubric_input,
    build_context_activation_arm,
    c6_win_for_a3,
    case_for_variant,
    context_activation_turn_argv,
    dry_run_lines,
    effective_bar_reading,
    estimate_session_count,
    generate_context_activation_episode,
    harness_fault_status,
    intersect_with_gold_poison,
    oracle_packet_text,
    reserve_budget,
    rotate_arm_order,
    score_reminder_turn,
    strike_rule_applies,
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


def test_rotate_arm_order_is_not_symmetric_in_seed_and_case_index() -> None:
    # minor: a plain (seed + case_index) % n is symmetric, so swapping the
    # two arguments silently produced the same rotation -- not the
    # independent rotation "seeded by (seed, case_index)" the design intends.
    assert rotate_arm_order(1, 2) != rotate_arm_order(2, 1)


def test_unknown_arm_id_refused() -> None:
    with pytest.raises(ArmSetupError):
        build_context_activation_arm("A99_bogus")


# -- per-arm Exomem availability is what actually distinguishes the arms --


def test_a1_control_has_no_exomem_surface() -> None:
    arm = build_context_activation_arm("A1_control")
    assert arm.allowed_tools == ""
    assert arm.uses_plugin is False


def test_a1_control_environment_carries_no_exomem_variables_at_all(tmp_path) -> None:
    # M6: A1 must have Exomem truly *absent* -- not merely denied via the
    # tool allowlist over an otherwise-normal isolated environment that
    # still points EXOMEM_CONFIG_PATH/EXOMEM_VAULT_PATH at a real directory.
    variant = variant_for_case("C2")
    episode = generate_context_activation_episode(9, variant)
    parent_env = {"PATH": "/usr/bin", "EXOMEM_VAULT_PATH": "/should/not/survive", "HOME": "/home/test"}
    plan = context_activation_turn_argv(
        episode=episode, arm_id="A1_control", envelope=_FakeEnvelope(), out_dir=tmp_path / "out", parent_env=parent_env
    )
    assert not any(key.startswith("EXOMEM_") for key in plan.env)


def test_other_arms_environment_does_carry_exomem_variables(tmp_path) -> None:
    variant = variant_for_case("C2")
    episode = generate_context_activation_episode(9, variant)
    plan = context_activation_turn_argv(
        episode=episode, arm_id="A2_raw_recall", envelope=_FakeEnvelope(), out_dir=tmp_path / "out", parent_env={}
    )
    assert any(key.startswith("EXOMEM_") for key in plan.env)


def test_a1_control_argv_carries_no_mcp_config_or_allowedtools_flag_at_all(tmp_path) -> None:
    # Spec (Requirement: Agent arms and controls): "A1 control with Exomem
    # absent, carrying no Exomem environment variable, no MCP configuration
    # and no tool allowlist flag" -- the flags themselves must be absent
    # from argv, not merely carry an empty value.
    variant = variant_for_case("C2")
    episode = generate_context_activation_episode(9, variant)
    plan = context_activation_turn_argv(
        episode=episode, arm_id="A1_control", envelope=_FakeEnvelope(), out_dir=tmp_path / "out", parent_env={}
    )
    assert "--mcp-config" not in plan.argv
    assert "--allowedTools" not in plan.argv
    assert "--strict-mcp-config" not in plan.argv


def test_other_arms_argv_does_carry_the_mcp_config_and_allowedtools_flags(tmp_path) -> None:
    variant = variant_for_case("C2")
    episode = generate_context_activation_episode(9, variant)
    plan = context_activation_turn_argv(
        episode=episode, arm_id="A2_raw_recall", envelope=_FakeEnvelope(), out_dir=tmp_path / "out", parent_env={}
    )
    assert "--mcp-config" in plan.argv
    assert "--allowedTools" in plan.argv


def test_a2_and_a4_both_expose_ask_memory_but_only_a4_is_nudged() -> None:
    a2 = build_context_activation_arm("A2_raw_recall")
    a4 = build_context_activation_arm("A4_nudged_recall")
    assert a2.allowed_tools == a4.allowed_tools == "mcp__exomem"
    variant = variant_for_case("C1")
    episode = generate_context_activation_episode(1, variant)
    assert system_prompt_text_for("A2_raw_recall", episode) is None
    assert system_prompt_text_for("A4_nudged_recall", episode) is not None


def test_a5_oracle_packet_is_the_fixtures_own_hand_authored_oracle_text() -> None:
    # B6: A5's ceiling packet reads `fixture.oracle_text` -- authored
    # independently of `must_include` -- never derived from must_include
    # itself (that would make A5 pass the reminder-turn test by
    # construction, since both would share one source).
    c1 = fixture_by_id("C1")
    assert oracle_packet_text(c1) == c1.oracle_text
    assert c1.oracle_text.strip()
    for key in c1.gold:
        assert key not in c1.oracle_text  # logical keys are evaluator plumbing, not prose


def test_a5_oracle_packet_is_empty_for_c6_the_no_memory_case() -> None:
    c6 = fixture_by_id("C6")
    assert c6.oracle_text == ""
    assert oracle_packet_text(c6) == ""


@pytest.mark.parametrize("twin_id", TWIN_IDS)
def test_a5_oracle_packet_is_empty_for_every_twin(twin_id: str) -> None:
    twin = fixture_by_id(twin_id)
    assert oracle_packet_text(twin) == ""


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


def test_reminder_turn_reflected_when_the_response_states_the_arithmetic_answer() -> None:
    # B4: C6's turn changed to a pure-arithmetic question with an exact
    # numeric answer ("9.5"), so its must_include fact is no longer vacuous.
    c6 = fixture_by_id("C6")
    assert c6.must_include == ("9.5",)
    assert score_reminder_turn("Half of nineteen is 9.5.", c6).reflected is True
    assert score_reminder_turn("That's roughly ten.", c6).reflected is False


def test_blind_intersection_matches_a_realistic_paraphrase_not_a_raw_key() -> None:
    # B5: a realistic grader reports prose it read from the response, never
    # a fixture's own internal logical keys -- the intersection must match
    # on the fixture's authored fact phrase (normalised, paraphrase-tolerant),
    # not on key-identity substring/equality.
    c1 = fixture_by_id("C1")
    extraction = ExtractedFacts(
        asserted=("mentions an AI subscriptions collection that tracks plan tiers", "unrelated fact"),
        requested=("asks about a step-count fitness goal unrelated to tooling",),
    )
    intersection = intersect_with_gold_poison(extraction, c1)
    assert intersection.asserted_gold == (extraction.asserted[0],)
    assert intersection.asserted_poison == ()
    assert intersection.requested_poison == (extraction.requested[0],)


def test_blind_intersection_does_not_match_on_the_raw_logical_key_alone() -> None:
    # A raw key is exactly what a grader that only ever saw the turn and the
    # response could not have produced; it must not match by construction.
    c1 = fixture_by_id("C1")
    extraction = ExtractedFacts(asserted=(c1.gold[0],), requested=())
    intersection = intersect_with_gold_poison(extraction, c1)
    assert intersection.asserted_gold == ()


# -- round-two BLOCKER (B5): stopwords, discriminating word, confusable sets -


_CONFUSABLE_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("C4", "c4_entity_profile", "t4_shared_first_name_entity_a"),
    ("C4", "c4_entity_profile", "t4_shared_first_name_entity_b"),
    ("T4", "t4_shared_first_name_entity_a", "t4_shared_first_name_entity_b"),
    ("C7", "c7_hub_feature", "c7_hub_market"),
    ("C7", "c7_hub_feature", "c7_hub_search_ux"),
    ("C7", "c7_hub_market", "c7_hub_search_ux"),
    ("C8", "c8_superseded_ancestor_1", "c8_superseded_ancestor_2"),
)


def test_confusable_facts_never_match_each_other_directly() -> None:
    # Direct unit-level proof (independent of which bucket a case sorts a
    # key into): the private matcher itself must refuse every confusable
    # pair, both directions.
    from membench.utility.context_activation_arms import _fact_phrase_matches

    for case_id, key_a, key_b in _CONFUSABLE_GROUPS:
        fixture = fixture_by_id(case_id)
        facts_by_key = {k: GOLD_POISON_FACTS[k] for k in (*fixture.gold, *fixture.poison) if k in GOLD_POISON_FACTS}
        assert not _fact_phrase_matches(GOLD_POISON_FACTS[key_a], key_b, facts_by_key), f"{key_a} matched {key_b}"
        assert not _fact_phrase_matches(GOLD_POISON_FACTS[key_b], key_a, facts_by_key), f"{key_b} matched {key_a}"


def test_every_fixtures_own_gold_phrase_never_registers_as_poison() -> None:
    # Across all 18 fixtures: asserting a case's own gold fact, verbatim,
    # must never come back tagged as poison use for that same case.
    for fixture in FIXTURES:
        for key in fixture.gold:
            if key not in GOLD_POISON_FACTS:
                continue
            extraction = ExtractedFacts(asserted=(GOLD_POISON_FACTS[key],), requested=())
            intersection = intersect_with_gold_poison(extraction, fixture)
            assert GOLD_POISON_FACTS[key] not in intersection.asserted_poison, (
                f"{fixture.case_id}: {key}'s own gold phrase registered as poison"
            )


_REALISTIC_PARAPHRASES: tuple[tuple[str, str, str], ...] = (
    # (case_id, key, realistic paraphrase asserting that key's own fact)
    ("C1", "c1_weekly_limit_insight", "the user keeps hitting their weekly usage cap"),
    ("C5", "c5_records_latest_unavailable", "the bench is unavailable, it's being repaired"),
    ("C1", "c1_subscriptions_collection", "mentions an AI subscriptions collection that tracks plan tiers"),
    ("C1", "t1_fitness_goal_note", "asks about a step-count fitness goal unrelated to tooling"),
    ("C8", "c8_active_head", "says the current onboarding approach is the one to use"),
)


@pytest.mark.parametrize("case_id,key,paraphrase", _REALISTIC_PARAPHRASES)
def test_realistic_paraphrases_match_their_own_fact_and_no_other(case_id: str, key: str, paraphrase: str) -> None:
    fixture = fixture_by_id(case_id)
    extraction = ExtractedFacts(asserted=(paraphrase,), requested=())
    intersection = intersect_with_gold_poison(extraction, fixture)
    own_bucket = intersection.asserted_gold if key in fixture.gold else intersection.asserted_poison
    other_bucket = intersection.asserted_poison if key in fixture.gold else intersection.asserted_gold
    assert paraphrase in own_bucket, f"{paraphrase!r} did not match its own fact {key}"
    assert paraphrase not in other_bucket, f"{paraphrase!r} also matched the wrong side"


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


# -- M1: cost cap and per-episode reservation -------------------------------


def test_reserve_budget_within_cap_returns_the_reservation() -> None:
    reserved = reserve_budget(10, cap_usd=1.0, reservation_usd=0.05)
    assert reserved == 0.5


def test_reserve_budget_over_cap_refuses_the_run() -> None:
    with pytest.raises(BudgetError):
        reserve_budget(1000, cap_usd=1.0, reservation_usd=0.05)


def test_reserve_budget_uses_the_pre_registered_defaults() -> None:
    session_count = estimate_session_count(case_ids=CASE_IDS + TWIN_IDS, arm_ids=ARM_IDS, repeats=1)
    reserved = reserve_budget(session_count)
    assert reserved == session_count * PER_EPISODE_RESERVATION_USD
    assert reserved <= COST_CAP_USD


# -- B6: the strike rule only applies where there is something to strike ---


def test_strike_rule_applies_to_resolved_and_ambiguous_cases_only() -> None:
    assert strike_rule_applies(fixture_by_id("C1")) is True  # resolved
    assert strike_rule_applies(fixture_by_id("C7")) is True  # ambiguous
    assert strike_rule_applies(fixture_by_id("C6")) is False  # unresolved


# -- round-two residue: C6's own narrower win rule for A3 -------------------


def test_c6_win_requires_a3_correct_with_zero_injection_and_search_while_a4_did_something() -> None:
    assert (
        c6_win_for_a3(a3_correct=True, a3_injected_chars=0, a3_memory_searches=0, a4_searched=True, a4_injected_chars=0)
        is True
    )


def test_c6_win_fails_if_a3_injected_anything() -> None:
    assert (
        c6_win_for_a3(a3_correct=True, a3_injected_chars=1, a3_memory_searches=0, a4_searched=True, a4_injected_chars=0)
        is False
    )


def test_c6_win_fails_if_a3_searched_memory() -> None:
    assert (
        c6_win_for_a3(a3_correct=True, a3_injected_chars=0, a3_memory_searches=1, a4_searched=True, a4_injected_chars=0)
        is False
    )


def test_c6_win_fails_if_a4_never_searched_or_injected() -> None:
    assert (
        c6_win_for_a3(
            a3_correct=True, a3_injected_chars=0, a3_memory_searches=0, a4_searched=False, a4_injected_chars=0
        )
        is False
    )


def test_c6_win_fails_if_a3_answered_incorrectly() -> None:
    assert (
        c6_win_for_a3(
            a3_correct=False, a3_injected_chars=0, a3_memory_searches=0, a4_searched=True, a4_injected_chars=0
        )
        is False
    )


# -- round-two residue: the effective-bar reading beside the count ---------


def _all_wins(**overrides: bool) -> dict[str, bool]:
    wins = dict.fromkeys(CASE_IDS, True)
    wins.update(overrides)
    return wins


def test_effective_bar_reading_meets_the_bar_when_everything_is_won() -> None:
    result = effective_bar_reading(_all_wins())
    assert result.raw_wins == 9
    assert result.distinct_query_wins == 8
    assert result.grill_query_won is True
    assert result.meets_bar is True


def test_effective_bar_reading_never_meets_the_bar_when_the_grill_query_is_lost() -> None:
    # Losing C2 (and, correlated, C9 -- the same query) leaves 7/9 raw wins,
    # numerically clearing MECHANISM_ACCEPT_BAR, but the grill query itself
    # must never be tolerated as the one loss.
    result = effective_bar_reading(_all_wins(C2=False, C9=False))
    assert result.raw_wins == MECHANISM_ACCEPT_BAR
    assert result.grill_query_won is False
    assert result.meets_bar is False


def test_effective_bar_reading_tolerates_losing_one_other_distinct_query() -> None:
    result = effective_bar_reading(_all_wins(C4=False))
    assert result.raw_wins == 8
    assert result.grill_query_won is True
    assert result.meets_bar is True


def test_effective_bar_reading_refuses_a_partial_case_set() -> None:
    with pytest.raises(ArmSetupError):
        effective_bar_reading({"C1": True})
