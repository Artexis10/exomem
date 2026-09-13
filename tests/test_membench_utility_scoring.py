"""Pure paired scorer: coverage accounting, harm/lift semantics, costs."""

from __future__ import annotations

import pytest

from membench.utility.schema import ActionOutcome, AttemptStatus, UsageRecord
from membench.utility.scoring import aggregate_usage, score_variant


def _outcome(arm: str, status: AttemptStatus, success: bool | None, *, destructive: tuple[str, ...] = ()) -> ActionOutcome:
    return ActionOutcome(
        episode_id="EPI-x",
        variant="helpful_history",
        arm=arm,
        status=status,
        success=success,
        destructive_effects=destructive,
    )


def _valid(arm: str, success: bool, **kw) -> ActionOutcome:
    return _outcome(arm, AttemptStatus.VALID, success, **kw)


def test_all_paired_success_combinations() -> None:
    pairs = [
        (_valid("control", True), _valid("memory", True)),   # both pass
        (_valid("control", False), _valid("memory", False)),  # both fail
        (_valid("control", False), _valid("memory", True)),   # win
        (_valid("control", True), _valid("memory", False)),   # loss
    ]
    pair_outcome, arm_outcomes = score_variant("helpful_history", pairs)
    assert pair_outcome.both_pass == 1
    assert pair_outcome.both_fail == 1
    assert pair_outcome.wins == 1
    assert pair_outcome.losses == 1
    assert pair_outcome.valid == 4
    assert pair_outcome.scheduled == 4
    assert pair_outcome.attempted == 4
    assert pair_outcome.missing == 0
    assert pair_outcome.invalid == 0
    # utility lift = mean(M - C) in percentage points = (wins - losses) / 4 * 100 = 0
    assert pair_outcome.utility_lift == 0.0
    # harm: control succeeded twice (both_pass, loss); memory failed once of those (loss)
    assert pair_outcome.harm_denominator == 2
    assert pair_outcome.harm_numerator == 1
    assert pair_outcome.conditional_harm == 0.5
    assert pair_outcome.unconditional_losses == 0.25
    for arm_name in ("control", "memory"):
        arm = arm_outcomes[arm_name]
        assert arm.attempted == 4
        assert arm.valid == 4
        assert arm.invalid == 0
        assert arm.missing == 0


def test_zero_control_success_denominator_is_undefined_not_zero() -> None:
    pairs = [
        (_valid("control", False), _valid("memory", False)),
        (_valid("control", False), _valid("memory", True)),
    ]
    pair_outcome, _ = score_variant("stale_distractor", pairs)
    assert pair_outcome.harm_denominator == 0
    assert pair_outcome.conditional_harm is None
    # both_fail + win counted, coverage still visible
    assert pair_outcome.both_fail == 1
    assert pair_outcome.wins == 1


def test_missing_and_invalid_arms_are_separated() -> None:
    pairs = [
        (_valid("control", True), _outcome("memory", AttemptStatus.MISSING, None)),
        (_outcome("control", AttemptStatus.INVALID, None), _valid("memory", True)),
        (_valid("control", True), _valid("memory", True)),
    ]
    pair_outcome, arm_outcomes = score_variant("helpful_history", pairs)
    assert pair_outcome.missing == 1
    assert pair_outcome.invalid == 1
    assert pair_outcome.valid == 1
    # The one-sided pair (control attempted, memory missing) still counts as
    # an attempt even though the pair itself cannot be scored.
    assert pair_outcome.attempted == 3
    assert pair_outcome.scheduled == 3
    # A missing/invalid pair contributes no win/loss/both_* classification.
    assert pair_outcome.both_pass == 1
    assert pair_outcome.wins == 0 and pair_outcome.losses == 0 and pair_outcome.both_fail == 0
    # The individually attempted side of an invalid pair is still retained.
    assert arm_outcomes["memory"].valid == 2  # the missing-pair partner + the both-pass pair
    assert arm_outcomes["control"].invalid == 1
    assert arm_outcomes["memory"].missing == 1


def test_every_control_fails_undefined_harm_with_visible_coverage() -> None:
    pairs = [
        (_valid("control", False), _valid("memory", False)),
        (_valid("control", False), _valid("memory", False)),
    ]
    pair_outcome, _ = score_variant("self_contained", pairs)
    assert pair_outcome.harm_denominator == 0
    assert pair_outcome.conditional_harm is None
    assert pair_outcome.both_fail == 2
    assert pair_outcome.valid == 2


def test_unscheduled_coverage_is_reported_without_imputing_success() -> None:
    pairs = [(_valid("control", True), _valid("memory", True))]
    pair_outcome, _ = score_variant("helpful_history", pairs, scheduled=5)
    assert pair_outcome.scheduled == 5
    assert pair_outcome.valid == 1
    assert pair_outcome.missing == 4


def test_per_arm_destructive_effect_counts_are_unconditional() -> None:
    pairs = [
        (_valid("control", True), _valid("memory", False, destructive=("other_project_modified",))),
        (_outcome("control", AttemptStatus.INVALID, None), _valid("memory", False, destructive=("other_project_modified",))),
    ]
    pair_outcome, arm_outcomes = score_variant("helpful_history", pairs)
    # Both-fail-pair and invalid-pair destructive effects are both retained.
    assert arm_outcomes["memory"].destructive_effect_counts == {"other_project_modified": 2}
    assert arm_outcomes["control"].destructive_effect_counts == {}


def test_per_family_separation_is_the_callers_responsibility() -> None:
    """score_variant never mixes variants: each call is scoped to one family."""

    helpful_pairs = [(_valid("control", True), _valid("memory", True))]
    stale_pairs = [(_valid("control", False), _valid("memory", False))]
    helpful_outcome, _ = score_variant("helpful_history", helpful_pairs)
    stale_outcome, _ = score_variant("stale_distractor", stale_pairs)
    assert helpful_outcome.variant == "helpful_history"
    assert stale_outcome.variant == "stale_distractor"
    assert helpful_outcome.both_pass == 1
    assert stale_outcome.both_fail == 1


def test_failure_and_invalid_costs_are_retained_never_summed_twice() -> None:
    records = [
        UsageRecord(
            episode_id="EPI-1", variant="helpful_history", arm="memory", phase="action",
            input_tokens=100, output_tokens=20, reasoning_tokens=5, wall_time_s=1.5,
        ),
        UsageRecord(
            episode_id="EPI-2", variant="helpful_history", arm="memory", phase="action",
            input_tokens=None, output_tokens=None,  # unknown cost from a failed/invalid attempt
        ),
        UsageRecord(
            episode_id="EPI-3", variant="helpful_history", arm="control", phase="experience",
            input_tokens=50, output_tokens=10,
        ),
    ]
    totals = aggregate_usage(records)
    assert totals["input_tokens"].total == 150
    assert totals["input_tokens"].known_count == 2
    assert totals["input_tokens"].unknown_count == 1
    # Reasoning tokens tracked independently of output tokens, never folded in.
    assert totals["reasoning_tokens"].total == 5
    assert totals["output_tokens"].total == 30
    # A field with no known values reports None, not an invented zero.
    assert totals["turns"].total is None
    assert totals["turns"].known_count == 0
    assert totals["turns"].unknown_count == 3


def test_product_and_budget_failures_remain_valid_scored_outcomes() -> None:
    """Zero capture/retrieval and declared budget exhaustion are VALID
    failures, not INVALID, and must remain in the denominator."""

    pairs = [
        (_valid("control", True), _valid("memory", False)),  # e.g. budget exhausted
    ]
    pair_outcome, _ = score_variant("helpful_history", pairs)
    assert pair_outcome.valid == 1
    assert pair_outcome.losses == 1
    assert pair_outcome.harm_denominator == 1
    assert pair_outcome.harm_numerator == 1
    assert pair_outcome.conditional_harm == 1.0


def test_peak_context_tokens_aggregate_by_max_not_sum() -> None:
    records = [
        UsageRecord(episode_id="EPI-1", variant="helpful_history", arm="memory", phase="action", peak_context_tokens=9000),
        UsageRecord(episode_id="EPI-2", variant="helpful_history", arm="memory", phase="action", peak_context_tokens=15000),
        UsageRecord(episode_id="EPI-3", variant="helpful_history", arm="control", phase="action", peak_context_tokens=None),
    ]
    totals = aggregate_usage(records)
    assert totals["peak_context_tokens"].total == 15000
    assert totals["peak_context_tokens"].known_count == 2
    assert totals["peak_context_tokens"].unknown_count == 1


def test_cost_usd_and_cumulative_context_tokens_sum_like_other_fields() -> None:
    records = [
        UsageRecord(episode_id="EPI-1", variant="helpful_history", arm="memory", phase="action",
                    cost_usd=0.01, cumulative_context_tokens=500),
        UsageRecord(episode_id="EPI-2", variant="helpful_history", arm="control", phase="action",
                    cost_usd=0.02, cumulative_context_tokens=None),
    ]
    totals = aggregate_usage(records)
    assert totals["cost_usd"].total == pytest.approx(0.03)
    assert totals["cumulative_context_tokens"].total == 500
    assert totals["cumulative_context_tokens"].unknown_count == 1


def test_swapped_pair_arms_are_refused_not_silently_scored() -> None:
    swapped = (_valid("memory", True), _valid("control", False))
    with pytest.raises(ValueError):
        score_variant("helpful_history", [swapped])


def test_mismatched_variant_pair_is_refused() -> None:
    control = _outcome("control", AttemptStatus.VALID, True)
    memory = ActionOutcome(
        episode_id="EPI-x", variant="stale_distractor", arm="memory",
        status=AttemptStatus.VALID, success=True,
    )
    with pytest.raises(ValueError):
        score_variant("helpful_history", [(control, memory)])


def test_one_sided_attempt_counts_toward_attempted_and_missing() -> None:
    pairs = [
        (
            _outcome("control", AttemptStatus.VALID, True),
            _outcome("memory", AttemptStatus.MISSING, None),
        ),
    ]
    pair_outcome, arm_outcomes = score_variant("helpful_history", pairs)
    assert pair_outcome.attempted == 1
    assert pair_outcome.missing == 1
    assert pair_outcome.valid == 0
    assert arm_outcomes["control"].attempted == 1
    assert arm_outcomes["memory"].missing == 1
