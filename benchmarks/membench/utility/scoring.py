"""Pure paired scorer over observed action outcomes.

No model judge, no I/O. Consumes :class:`~membench.utility.schema.
ActionOutcome` pairs already produced by an :class:`~membench.utility.
action_world.ActionWorld`. Failures, invalid attempts and missing attempts
are never dropped; nested reasoning-token usage is never summed twice (see
:func:`aggregate_usage`, which sums each field independently and only over
non-``None`` values).
"""

from __future__ import annotations

from membench.utility.schema import (
    ActionOutcome,
    ArmOutcome,
    AttemptStatus,
    PairOutcome,
    UsageRecord,
    UsageTotal,
)

_USAGE_SUM_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "wall_time_s",
    "model_time_s",
    "tool_time_s",
    "turns",
    "cumulative_context_tokens",
    "cost_usd",
)
#: Aggregated by maximum, never summed: a peak is a high-water mark across
#: the same envelope, not an additive cost.
_USAGE_MAX_FIELDS: tuple[str, ...] = ("peak_context_tokens",)
_USAGE_FIELDS: tuple[str, ...] = _USAGE_SUM_FIELDS + _USAGE_MAX_FIELDS


def _validate_pair_identity(control: ActionOutcome, memory: ActionOutcome) -> None:
    """Refuse a swapped or malformed pair instead of silently scoring it.

    Checks the two attempts belong to *one* pair (same episode and variant,
    arms not swapped) -- not that they match the caller's ``variant`` label,
    which groups otherwise-unrelated pairs into one family score.
    """

    if control.arm != "control" or memory.arm != "memory":
        raise ValueError(f"pair arms are swapped or malformed: {control.arm!r}/{memory.arm!r}")
    if control.variant != memory.variant:
        raise ValueError("pair variant identities do not match")
    if control.episode_id != memory.episode_id:
        raise ValueError("pair episode identities do not match")


def _pair_status(control: ActionOutcome, memory: ActionOutcome) -> AttemptStatus:
    if AttemptStatus.MISSING in (control.status, memory.status):
        return AttemptStatus.MISSING
    if AttemptStatus.INVALID in (control.status, memory.status):
        return AttemptStatus.INVALID
    return AttemptStatus.VALID


def score_variant(
    variant: str,
    pairs: list[tuple[ActionOutcome, ActionOutcome]],
    *,
    scheduled: int | None = None,
) -> tuple[PairOutcome, dict[str, ArmOutcome]]:
    """Score every ``(control, memory)`` attempt pair for one variant.

    ``scheduled`` may exceed ``len(pairs)`` to record coverage that was never
    started at all (e.g. a budget cap); those unattempted pairs are folded
    into ``missing`` without imputing a success or failure.
    """

    scheduled_count = len(pairs) if scheduled is None else scheduled
    if scheduled_count < len(pairs):
        raise ValueError("scheduled cannot be smaller than the supplied pair count")

    arm_stats: dict[str, dict[str, object]] = {
        arm: {"attempted": 0, "valid": 0, "invalid": 0, "missing": 0, "destructive": {}}
        for arm in ("control", "memory")
    }
    valid_pairs = invalid_pairs = missing_pairs = one_sided_pairs = 0
    wins = losses = both_pass = both_fail = 0
    harm_numerator = harm_denominator = 0

    for control, memory in pairs:
        _validate_pair_identity(control, memory)
        for arm_name, outcome in (("control", control), ("memory", memory)):
            stats = arm_stats[arm_name]
            if outcome.status is AttemptStatus.MISSING:
                stats["missing"] = stats["missing"] + 1  # type: ignore[operator]
            else:
                stats["attempted"] = stats["attempted"] + 1  # type: ignore[operator]
                key = "valid" if outcome.status is AttemptStatus.VALID else "invalid"
                stats[key] = stats[key] + 1  # type: ignore[operator]
            destructive = stats["destructive"]
            assert isinstance(destructive, dict)
            for effect in outcome.destructive_effects:
                destructive[effect] = destructive.get(effect, 0) + 1

        status = _pair_status(control, memory)
        if status is AttemptStatus.MISSING:
            missing_pairs += 1
            both_missing = control.status is AttemptStatus.MISSING and memory.status is AttemptStatus.MISSING
            if not both_missing:
                one_sided_pairs += 1
            continue
        if status is AttemptStatus.INVALID:
            invalid_pairs += 1
            continue
        valid_pairs += 1
        control_success = bool(control.success)
        memory_success = bool(memory.success)
        if control_success and memory_success:
            both_pass += 1
        elif not control_success and not memory_success:
            both_fail += 1
        elif memory_success:
            wins += 1
        else:
            losses += 1
        if control_success:
            harm_denominator += 1
            if not memory_success:
                harm_numerator += 1

    unscheduled = scheduled_count - len(pairs)
    utility_lift = 100.0 * (wins - losses) / valid_pairs if valid_pairs else None
    conditional_harm = harm_numerator / harm_denominator if harm_denominator else None
    unconditional_losses = losses / valid_pairs if valid_pairs else None

    pair_outcome = PairOutcome(
        variant=variant,
        scheduled=scheduled_count,
        attempted=valid_pairs + invalid_pairs + one_sided_pairs,
        valid=valid_pairs,
        invalid=invalid_pairs,
        missing=missing_pairs + unscheduled,
        wins=wins,
        losses=losses,
        both_pass=both_pass,
        both_fail=both_fail,
        utility_lift=utility_lift,
        harm_numerator=harm_numerator,
        harm_denominator=harm_denominator,
        conditional_harm=conditional_harm,
        unconditional_losses=unconditional_losses,
    )
    arm_outcomes = {
        arm: ArmOutcome(
            variant=variant,
            arm=arm,
            attempted=stats["attempted"],  # type: ignore[arg-type]
            valid=stats["valid"],  # type: ignore[arg-type]
            invalid=stats["invalid"],  # type: ignore[arg-type]
            missing=stats["missing"],  # type: ignore[arg-type]
            destructive_effect_counts=dict(stats["destructive"]),  # type: ignore[arg-type]
        )
        for arm, stats in arm_stats.items()
    }
    return pair_outcome, arm_outcomes


def aggregate_usage(records: list[UsageRecord]) -> dict[str, UsageTotal]:
    """Sum each usage field independently over non-``None`` values only."""

    totals: dict[str, UsageTotal] = {}
    for field_name in _USAGE_FIELDS:
        known = [
            value
            for record in records
            if (value := getattr(record, field_name)) is not None
        ]
        reduce = max if field_name in _USAGE_MAX_FIELDS else sum
        totals[field_name] = UsageTotal(
            total=reduce(known) if known else None,
            known_count=len(known),
            unknown_count=len(records) - len(known),
        )
    return totals
