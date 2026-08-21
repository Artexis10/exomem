"""The write-latency gate must not fail the build on one noisy sample set.

Real incident, 2026-07-27: a contended CI runner produced a 2k baseline that was
*faster* than normal and an 8k measurement that was roughly double normal. The
scaling bound is anchored to the same run's small-corpus median, so both ends
moved the wrong way at once and the gate failed a release PR that had touched
nothing but a version string. A re-run passed with ordinary numbers.

That was mitigated by re-measuring on failure, and the sensitivity itself was
pinned here rather than removed. On 2026-08-20 it recurred and the mitigation
did not hold: both attempts failed on a branch whose only product changes were
a thread join on shutdown and a hook that does not run on the write path.

Twelve consecutive CI runs sampled at that point put the numbers beyond
argument. The 8k commit median -- the quantity the scaling rule bounds -- ranged
265.7-454.7ms. The bound it was compared against ranged 420.6-488.2ms across the
same runs (excluding one 1238.0ms outlier). The bound sat INSIDE the natural
spread of the thing it bounds, so which side of it a run landed on was decided
by the runner, not by the code. Four of the twelve came within 100ms of failing
and one landed at -3.9ms.

The observed ratio tells the same story: 8k/2k commit ran a median 2.55x against
a SCALING_RATIO of 2.0, so the rule was already carried entirely by
SCALING_SLACK_MS. Commit really does scale about 2.5x over a 4x corpus here.

So the bound is now floored at a fraction of each operation's own absolute
ceiling. That is a deliberate, narrowed claim: on this runner the scaling check
discriminates only ABOVE the noise floor -- between 600ms and the 750ms ceiling
for commit -- and the absolute ceilings are the primary bound below that.
Claiming finer resolution than the measurement supports is what produced two
false failures.

On 2026-08-21 it recurred a third time, in the rows the floor was withheld from.
`read_after_write` failed both attempts on main at 2a749c84 -- and its 8k figure,
the quantity the rule bounds, was at or below the running median across nine
consecutive runs. What moved was the 2k baseline it divides by: #718's post-RRF
early exit fires hard on a small corpus and barely on a large one, so the small
end improved roughly three times as much as the large end and the ratio rose
from a stable 6.6-8.3x to 12.3x. Nothing regressed. The gate cannot tell an
improved denominator from a regressed numerator, so it failed the build for
being faster.

The withheld reasoning was that the read/cold rows are "deliberately O(N) and
carry their own ratios". That is about ratio MAGNITUDE. The defect is baseline
NOISE -- the read row's own 2k sample spans 4.1x across those nine runs, moving
its bound by 16.5s while the value it bounds spans 12.7s. Being O(N) does not
exempt a row from having a noisy ruler, so all three now carry the same floor.

These tests pin the floor, pin that every historical false-failure sample set
now passes, pin that each floor is load-bearing rather than decorative, and pin
that genuine super-linear growth still fails before the absolute ceiling catches
it.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "semantic_write_latency.py"


def load_module():
    spec = importlib.util.spec_from_file_location("semantic_write_latency_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def result(
    pages: int,
    *,
    commit: float,
    validate: float = 30.0,
    read_after_write: float = 500.0,
    cold_read_after_write: float = 800.0,
    cold_preflight: float = 900.0,
) -> dict[str, float | int]:
    return {
        "pages": pages,
        "samples": 5,
        "cold_ms": 1000.0,
        "validate_median_ms": validate,
        "validate_p95_ms": validate * 1.4,
        "commit_median_ms": commit,
        "commit_p95_ms": commit * 1.15,
        "read_after_write_median_ms": read_after_write,
        "read_after_write_p95_ms": read_after_write * 1.4,
        "cold_read_after_write_ms": cold_read_after_write,
        "cold_preflight_ms": cold_preflight,
    }


HEALTHY = [result(2_000, commit=51.5), result(8_000, commit=191.7)]
# The numbers from the 2026-07-27 false failure.
NOISY = [result(2_000, commit=42.3), result(8_000, commit=365.6)]
# The 2026-08-20 recurrence, both attempts, measured on a branch that changed
# nothing on the write path.
RECURRENCE = [
    [result(2_000, commit=125.4, validate=37.0), result(8_000, commit=454.7, validate=142.3)],
    [result(2_000, commit=117.2, validate=40.0), result(8_000, commit=482.7, validate=134.9)],
]
# The widest validate spread seen in the same twelve runs: a 253.2ms 8k median
# against a 38.7ms baseline cleared its bound by 24.2ms. Validate carries the
# same defect as commit; it had simply not landed on the wrong side yet.
VALIDATE_MARGIN = [
    result(2_000, commit=115.9, validate=38.7),
    result(8_000, commit=287.8, validate=253.2),
]
# Genuine super-linear growth: above the floor, still under the absolute
# ceiling, so only the scaling rule can catch it.
SUPERLINEAR = [result(2_000, commit=130.0), result(8_000, commit=700.0)]

# The 2026-08-21 recurrence, one row down. Both attempts of the run that failed
# main at 2a749c84, plus attempt 1 at 2d40a8b2 -- all three verbatim from CI.
# Every 8k figure here is at or below the nine-run running median; what moved
# was the 2k baseline, because #718's post-RRF early exit fires hard on a small
# corpus and barely on a large one. The gate read the improvement as a
# regression in the row it divides by.
READ_ROW_RECURRENCE = [
    [
        result(
            2_000,
            commit=149.5,
            validate=20.9,
            read_after_write=671.8,
            cold_read_after_write=1_633.8,
            cold_preflight=2_669.3,
        ),
        result(
            8_000,
            commit=393.0,
            validate=86.8,
            read_after_write=8_291.7,
            cold_read_after_write=7_930.5,
            cold_preflight=11_885.0,
        ),
    ],
    [
        result(
            2_000,
            commit=258.9,
            validate=23.8,
            read_after_write=746.6,
            cold_read_after_write=1_845.3,
            cold_preflight=2_647.1,
        ),
        result(
            8_000,
            commit=413.7,
            validate=81.4,
            read_after_write=8_824.8,
            cold_read_after_write=8_369.3,
            cold_preflight=10_689.1,
        ),
    ],
    [
        result(
            2_000,
            commit=279.2,
            validate=29.2,
            read_after_write=778.7,
            cold_read_after_write=2_061.5,
            cold_preflight=4_634.6,
        ),
        result(
            8_000,
            commit=477.9,
            validate=161.5,
            read_after_write=9_710.0,
            cold_read_after_write=8_843.1,
            cold_preflight=15_930.7,
        ),
    ],
]

# The worst UNREGRESSED large-corpus figure seen across the same nine runs. Each
# floor has to clear its own entry here, or the gate is still inside its noise.
WORST_OBSERVED_8K = {
    "read_after_write_median_ms": 19_305.3,
    "cold_read_after_write_ms": 11_614.9,
    "cold_preflight_ms": 22_738.5,
}


def test_healthy_scaling_passes() -> None:
    load_module().check(HEALTHY)


def test_absolute_ceiling_breach_fails() -> None:
    module = load_module()
    with pytest.raises(SystemExit, match="commit_median_ms"):
        module.check([result(8_000, commit=module.COMMIT_MEDIAN_MS + 1)])


@pytest.mark.parametrize(
    "key",
    [
        "read_after_write_median_ms",
        "read_after_write_p95_ms",
        "cold_read_after_write_ms",
        "cold_preflight_ms",
    ],
)
def test_missing_new_key_fails_loudly(key: str) -> None:
    """check() hard-indexes every gate key -- a result dict missing one must
    fail loudly, not silently pass via a `.get()` skip (the exact
    invisibility class this lane closes: a relocated cost that never lands
    on any row would otherwise pass the build unnoticed)."""
    module = load_module()
    incomplete = result(2_000, commit=51.5)
    del incomplete[key]
    with pytest.raises(KeyError, match=key):
        module.check([incomplete])


def test_superlinear_scaling_fails() -> None:
    """Growth the runner cannot explain still fails, and fails on scaling.

    700ms at 8k is under COMMIT_MEDIAN_MS, so the absolute ceiling cannot catch
    it -- if this passes, the scaling rule has stopped doing anything.
    """
    with pytest.raises(SystemExit, match="commit scaling"):
        load_module().check(SUPERLINEAR)


@pytest.mark.parametrize("sample", [NOISY, *RECURRENCE, VALIDATE_MARGIN, *READ_ROW_RECURRENCE])
def test_measured_false_failures_pass(sample: list[dict[str, float | int]]) -> None:
    """Every sample set that has ever failed this gate falsely now passes.

    All four came off contended runners on trees with no write-path change.
    """
    load_module().check(sample)


def test_a_fast_baseline_cannot_tighten_the_bound_below_the_floor() -> None:
    """The 2026-07-27 sensitivity, now bounded rather than merely documented.

    A quicker small-corpus run still lowers the ratio term -- that part is
    arithmetic and unchanged -- but it can no longer drag the effective bound
    underneath what an unregressed large-corpus measurement produces on this
    runner.
    """
    module = load_module()

    def ratio_term(small: float) -> float:
        return small * module.SCALING_RATIO + module.SCALING_SLACK_MS

    def bound(small: float, ceiling: float) -> float:
        return max(ratio_term(small), ceiling * module.SCALING_BOUND_CEILING_FRACTION)

    # The ratio term still moves with the baseline, as it always did.
    assert ratio_term(42.3) == pytest.approx(284.6)
    assert ratio_term(51.5) == pytest.approx(303.0)
    assert ratio_term(42.3) < ratio_term(51.5)

    # The bound the gate actually applies does not follow it down.
    floor = module.COMMIT_MEDIAN_MS * module.SCALING_BOUND_CEILING_FRACTION
    assert bound(42.3, module.COMMIT_MEDIAN_MS) == pytest.approx(floor)
    assert bound(125.4, module.COMMIT_MEDIAN_MS) == pytest.approx(floor)

    # 454.7ms is the worst 8k commit median observed across twelve runs on an
    # unregressed tree. The floor has to clear it, or the gate is still inside
    # its own noise.
    assert floor > 454.7

    # And the floor must stay strictly under the absolute ceiling, or the
    # scaling rule has been reduced to a duplicate of it.
    assert floor < module.COMMIT_MEDIAN_MS
    assert (
        module.VALIDATE_MEDIAN_MS * module.SCALING_BOUND_CEILING_FRACTION
        < module.VALIDATE_MEDIAN_MS
    )


# (floor constant, ratio, slack, absolute ceiling) per O(N) row.
ON_ROWS = [
    (
        "read_after_write_median_ms",
        "READ_AFTER_WRITE_SCALING_BOUND_FLOOR_MS",
        "READ_AFTER_WRITE_SCALING_RATIO",
        "READ_AFTER_WRITE_SCALING_SLACK_MS",
        "READ_AFTER_WRITE_MEDIAN_MS",
    ),
    (
        "cold_read_after_write_ms",
        "COLD_READ_AFTER_WRITE_SCALING_BOUND_FLOOR_MS",
        "COLD_READ_AFTER_WRITE_SCALING_RATIO",
        "COLD_READ_AFTER_WRITE_SCALING_SLACK_MS",
        "COLD_READ_AFTER_WRITE_MS",
    ),
    (
        "cold_preflight_ms",
        "COLD_PREFLIGHT_SCALING_BOUND_FLOOR_MS",
        "COLD_PREFLIGHT_SCALING_RATIO",
        "COLD_PREFLIGHT_SCALING_SLACK_MS",
        "COLD_PREFLIGHT_MS",
    ),
]


@pytest.mark.parametrize("key,floor_name,ratio_name,slack_name,ceiling_name", ON_ROWS)
def test_each_on_row_floor_clears_its_worst_unregressed_sample(
    key: str, floor_name: str, ratio_name: str, slack_name: str, ceiling_name: str
) -> None:
    """The O(N) rows get the same floor, for the same reason as commit.

    Being O(N) justifies a larger RATIO. It does nothing about baseline noise,
    which is what actually fails the build -- the bound is anchored to the small
    sample either way. So each floor must clear the worst large-corpus figure an
    unregressed tree has produced on this runner, and must stay strictly under
    its own absolute ceiling so the scaling rule still fires first.
    """
    module = load_module()
    floor = getattr(module, floor_name)

    assert floor > WORST_OBSERVED_8K[key]
    assert floor < getattr(module, ceiling_name)

    # The ratio term still moves with the baseline -- unchanged arithmetic --
    # but it can no longer drag the applied bound below the floor.
    ratio, slack = getattr(module, ratio_name), getattr(module, slack_name)
    fast, slow = 100.0, 5_000.0
    assert fast * ratio + slack < floor  # a fast baseline is floored
    assert max(fast * ratio + slack, floor) == floor
    assert max(slow * ratio + slack, floor) == slow * ratio + slack  # slow still rules


@pytest.mark.parametrize("key,floor_name,ratio_name,slack_name,ceiling_name", ON_ROWS)
def test_the_floor_is_load_bearing_on_every_on_row(
    key: str, floor_name: str, ratio_name: str, slack_name: str, ceiling_name: str
) -> None:
    """Remove any one floor and this must go red.

    The read row is covered by READ_ROW_RECURRENCE, which is measured. The two
    cold rows have not landed on the wrong side of their bound in CI yet -- they
    are nearer than they look (cold_preflight's bare bound clears its worst
    observed 8k figure by 16%), and they read the same candidate path #718 just
    made faster, so they move next. Rather than ship two floors no test
    exercises, construct the case per row: a baseline fast enough that the bare
    ratio term fails a large figure the floor and the ceiling both accept.
    """
    module = load_module()
    ratio, slack = getattr(module, ratio_name), getattr(module, slack_name)
    floor, ceiling = getattr(module, floor_name), getattr(module, ceiling_name)

    fast_small = (floor - slack) / ratio * 0.55
    big_large = (fast_small * ratio + slack + floor) / 2.0

    # The case is only meaningful if the bare term fails where the floor saves.
    assert fast_small * ratio + slack <= big_large, "bare bound must fail this sample"
    assert big_large < floor < ceiling, "floor must save it, ceiling must allow it"

    small, large = result(2_000, commit=130.0), result(8_000, commit=200.0)
    small[key], large[key] = fast_small, big_large
    module.check([small, large])  # passes only because the row is floored


@pytest.mark.parametrize("key,floor_name,_r,_s,ceiling_name", ON_ROWS)
def test_a_real_regression_on_an_on_row_still_fails(
    key: str, floor_name: str, _r: str, _s: str, ceiling_name: str
) -> None:
    """The floor must not neuter the row it protects.

    Growth past the floor but under the absolute ceiling can only be caught by
    the scaling rule -- if this passes, flooring turned the check off.
    """
    module = load_module()
    floor = getattr(module, floor_name)
    assert floor + 1.0 < getattr(module, ceiling_name), "sample must clear the ceiling test"

    small = result(2_000, commit=130.0)
    large = result(8_000, commit=200.0)
    large[key] = floor + 1.0
    with pytest.raises(SystemExit, match=f"{key.removesuffix('_median_ms').removesuffix('_ms')} scaling"):
        module.check([small, large])


def test_check_failure_is_confirmed_by_a_second_measurement(monkeypatch) -> None:
    """A first-attempt failure re-measures; a clean second attempt passes."""

    module = load_module()
    attempts: list[int] = []

    def fake_measure_all(sizes, samples, root, **_kwargs):
        attempts.append(len(attempts) + 1)
        return SUPERLINEAR if len(attempts) == 1 else HEALTHY

    monkeypatch.setattr(module, "measure_all", fake_measure_all)
    assert module.main(["--check"]) == 0
    assert attempts == [1, 2], "a failing first attempt must be re-measured once"


def test_a_reproducible_failure_still_fails(monkeypatch) -> None:
    """Confirmation must not turn a real regression into a pass."""

    module = load_module()
    attempts: list[int] = []

    def fake_measure_all(sizes, samples, root, **_kwargs):
        attempts.append(len(attempts) + 1)
        return SUPERLINEAR

    monkeypatch.setattr(module, "measure_all", fake_measure_all)
    with pytest.raises(SystemExit, match="commit scaling"):
        module.main(["--check"])
    assert attempts == [1, 2], "exactly two attempts before failing the build"


def test_attempts_one_disables_confirmation(monkeypatch) -> None:
    module = load_module()
    attempts: list[int] = []

    def fake_measure_all(sizes, samples, root, **_kwargs):
        attempts.append(len(attempts) + 1)
        return SUPERLINEAR

    monkeypatch.setattr(module, "measure_all", fake_measure_all)
    with pytest.raises(SystemExit, match="commit scaling"):
        module.main(["--check", "--attempts", "1"])
    assert attempts == [1]


def test_no_check_flag_never_fails(monkeypatch) -> None:
    module = load_module()
    monkeypatch.setattr(
        module, "measure_all", lambda sizes, samples, root, **_kwargs: SUPERLINEAR
    )
    assert module.main([]) == 0
