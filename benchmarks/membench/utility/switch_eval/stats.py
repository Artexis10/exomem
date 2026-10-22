"""The decision statistic: sign-flip tests over groups, the inverted interval, power.

Design, "Statistic and decision rule": the statistic is the per-case score
difference averaged over cases; within a group the case differences sum, so
the statistic stays the mean over cases while the test flips whole groups.
Superiority is the one-sided test at 0.025; non-inferiority within δ is the
same test on the difference plus δ; the 95% interval inverts the test.

Every function here is deterministic: an exact enumeration of the sign
patterns where that is small, else a seeded Monte Carlo draw, recorded with
the result so a reader recomputes the same p-value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from . import protocol

#: Harness knob, not a protocol value: up to this many groups the test
#: enumerates every sign pattern (2^20 patterns, in chunks).
EXACT_GROUP_LIMIT = 20
#: Harness knob: sign draws for a Monte Carlo test past the exact limit. At a
#: p-value near 0.025 the draw error is about 0.00035.
MONTE_CARLO_DRAWS = 200_000
_CHUNK = 1 << 16
_EPS = 1e-9


@dataclass(frozen=True)
class Grouped:
    """Per-group sums of case differences, and how many cases each group holds."""

    groups: tuple[str, ...]
    sums: np.ndarray
    sizes: np.ndarray

    @property
    def cases(self) -> int:
        return int(self.sizes.sum())

    @property
    def mean(self) -> float:
        return float(self.sums.sum() / self.cases) if self.cases else float("nan")


def group_differences(differences: Mapping[str, float], groups: Mapping[str, str]) -> Grouped:
    """Sum each group's case differences; ``groups`` maps case id to group."""

    sums: dict[str, float] = {}
    sizes: dict[str, int] = {}
    for case_id, difference in differences.items():
        group = groups[case_id]
        sums[group] = sums.get(group, 0.0) + float(difference)
        sizes[group] = sizes.get(group, 0) + 1
    names = tuple(sorted(sums))
    return Grouped(
        names,
        np.array([sums[g] for g in names], dtype=float),
        np.array([sizes[g] for g in names], dtype=float),
    )


@dataclass(frozen=True)
class TestResult:
    statistic: float
    p_value: float
    groups: int
    cases: int
    shift: float
    method: str

    def as_dict(self) -> dict:
        return {
            "statistic": self.statistic,
            "p_value": self.p_value,
            "groups": self.groups,
            "cases": self.cases,
            "shift": self.shift,
            "method": self.method,
        }


class SignFlip:
    """The sign patterns for one set of groups, reused for every shift.

    Reusing one set of patterns keeps the p-value monotone in the shift, which
    the interval inversion relies on.
    """

    def __init__(self, grouped: Grouped, *, seed: int) -> None:
        self.grouped = grouped
        count = len(grouped.groups)
        if count <= EXACT_GROUP_LIMIT:
            self.exact = True
            self.method = f"exact over {2**count} sign patterns"
            self._patterns = 2**count
        else:
            self.exact = False
            self.method = f"Monte Carlo, {MONTE_CARLO_DRAWS} draws, seed {seed}"
            rng = np.random.default_rng(seed)
            self._signs = rng.integers(0, 2, size=(MONTE_CARLO_DRAWS, count), dtype=np.int8) * 2 - 1

    def _flipped_chunks(self, values: np.ndarray):
        count = len(values)
        if self.exact:
            bits = np.arange(count, dtype=np.int64)
            for start in range(0, self._patterns, _CHUNK):
                index = np.arange(start, min(start + _CHUNK, self._patterns), dtype=np.int64)
                signs = 1.0 - 2.0 * ((index[:, None] >> bits[None, :]) & 1)
                yield signs @ values
        else:
            for start in range(0, len(self._signs), _CHUNK):
                yield self._signs[start : start + _CHUNK].astype(float) @ values

    def p_value(self, shift: float = 0.0, *, tail: str = "upper") -> float:
        """One-sided p-value of the mean difference plus ``shift`` per case."""

        values = self.grouped.sums + shift * self.grouped.sizes
        observed = float(values.sum())
        hits = 0
        for flipped in self._flipped_chunks(values):
            if tail == "upper":
                hits += int(np.count_nonzero(flipped >= observed - _EPS))
            else:
                hits += int(np.count_nonzero(flipped <= observed + _EPS))
        if self.exact:
            return hits / self._patterns
        return (1 + hits) / (len(self._signs) + 1)

    def test(self, shift: float = 0.0) -> TestResult:
        return TestResult(
            statistic=self.grouped.mean,
            p_value=self.p_value(shift),
            groups=len(self.grouped.groups),
            cases=self.grouped.cases,
            shift=shift,
            method=self.method,
        )

    def interval(
        self, *, alpha: float = protocol.ALPHA_ONE_SIDED
    ) -> tuple[float | None, float | None]:
        """The interval of mean differences the two one-sided tests do not reject.

        ``None`` marks a side the test cannot bound: with few groups even the
        most extreme data does not reach ``alpha``.
        """

        sums, sizes = self.grouped.sums, self.grouped.sizes
        per_case = sums / sizes
        low_start = float(per_case.min()) - 1.0
        high_start = float(per_case.max()) + 1.0
        mean = self.grouped.mean

        def rejects_below(theta: float) -> bool:  # H0: mean <= theta
            return self.p_value(-theta, tail="upper") <= alpha

        def rejects_above(theta: float) -> bool:  # H0: mean >= theta
            return self.p_value(-theta, tail="lower") <= alpha

        lower = None
        if rejects_below(low_start):
            lo, hi = low_start, mean
            for _ in range(60):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if rejects_below(mid) else (lo, mid)
                if hi - lo < 1e-6:
                    break
            lower = round(lo, 6)
        upper = None
        if rejects_above(high_start):
            lo, hi = mean, high_start
            for _ in range(60):
                mid = (lo + hi) / 2
                lo, hi = (lo, mid) if rejects_above(mid) else (mid, hi)
                if hi - lo < 1e-6:
                    break
            upper = round(hi, 6)
        return lower, upper


def compare(
    differences: Mapping[str, float],
    groups: Mapping[str, str],
    *,
    seed: int,
    delta: float | None = None,
    alpha: float = protocol.ALPHA_ONE_SIDED,
) -> dict:
    """Superiority, and non-inferiority within ``delta`` when given, with the interval."""

    if not differences:
        return {"evaluable": False, "reason": "no cases"}
    flip = SignFlip(group_differences(differences, groups), seed=seed)
    superiority = flip.test()
    result = {
        "evaluable": True,
        "mean_difference": superiority.statistic,
        "superiority": {**superiority.as_dict(), "passes": superiority.p_value <= alpha},
        "interval_95": flip.interval(alpha=alpha),
    }
    if delta is not None:
        shifted = flip.test(delta)
        result["non_inferiority"] = {
            **shifted.as_dict(),
            "delta": delta,
            "passes": shifted.p_value <= alpha,
        }
    return result


# --------------------------------------------------------------------------
# Power.
# --------------------------------------------------------------------------


def repeat_spread(repeat_scores: Mapping[str, Mapping[str, Sequence[float]]]) -> float:
    """Mean over cases of the per-arm standard deviation between repeats."""

    spreads = [
        float(np.std(np.asarray(scores, dtype=float)))
        for arms in repeat_scores.values()
        for scores in arms.values()
        if len(scores) > 1
    ]
    return float(np.mean(spreads)) if spreads else float("nan")


def simulate_pass_rate(
    case_rates: Sequence[float],
    *,
    size: int,
    delta: float,
    seed: int,
    repeats: int = protocol.REPEATS,
    simulations: int = 2000,
    flips: int = 4000,
    alpha: float = protocol.ALPHA_ONE_SIDED,
) -> float:
    """How often the non-inferiority test passes for two equal arms.

    Each simulated case draws a development case's pass rate (its mean over all
    arms and repeats, so the arms are equal by construction), both arms draw
    ``repeats`` pass-or-fail repeats from it, and each case is its own group.
    """

    rates = np.asarray(case_rates, dtype=float)
    if rates.size == 0 or size < 1:
        return float("nan")
    rng = np.random.default_rng(seed)
    passes = 0
    for _ in range(simulations):
        chosen = rng.choice(rates, size=size, replace=True)
        first = rng.binomial(repeats, chosen) / repeats
        second = rng.binomial(repeats, chosen) / repeats
        values = first - second + delta
        signs = rng.integers(0, 2, size=(flips, size)).astype(float) * 2.0 - 1.0
        hits = int(np.count_nonzero(signs @ values >= values.sum() - _EPS))
        passes += (1 + hits) / (flips + 1) <= alpha
    return passes / simulations


def power_step(
    case_rates: Sequence[float],
    *,
    sizes: Sequence[int],
    available: int,
    seed: int,
    simulations: int = 2000,
) -> dict:
    """The protocol's power step for one rule (design, "Power", rules 2 and 3).

    Raise the held-out size first, at δ = 0.10; only when every available turn
    still misses the 80% pass rate, raise δ in steps of 0.01 up to 0.15.
    """

    table = []
    chosen = None
    for size in sorted({*sizes, available}):
        if size > available:
            continue
        rate = simulate_pass_rate(
            case_rates, size=size, delta=protocol.DELTA_FLOOR, seed=seed, simulations=simulations
        )
        table.append({"size": size, "delta": protocol.DELTA_FLOOR, "pass_rate": rate})
        if chosen is None and rate >= protocol.POWER_TARGET:
            chosen = {"size": size, "delta": protocol.DELTA_FLOOR, "pass_rate": rate}
    if chosen is None:
        steps = round((protocol.DELTA_CAP - protocol.DELTA_FLOOR) / 0.01)
        for step in range(1, steps + 1):
            delta = round(protocol.DELTA_FLOOR + 0.01 * step, 2)
            rate = simulate_pass_rate(
                case_rates, size=available, delta=delta, seed=seed, simulations=simulations
            )
            table.append({"size": available, "delta": delta, "pass_rate": rate})
            if rate >= protocol.POWER_TARGET:
                chosen = {"size": available, "delta": delta, "pass_rate": rate}
                break
    return {
        "development_cases": len(case_rates),
        "table": table,
        "chosen": chosen,
        "short_at_cap": chosen is None,
        "simulations": simulations,
        "seed": seed,
    }
