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

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from . import protocol
from .cases import KIND_MISS, KIND_ORDINARY

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
# Power (design, "Power").
# --------------------------------------------------------------------------

#: Harness knob, not a protocol value: the step by which δ rises from the floor.
DELTA_STEP = 0.01
#: Harness knob: sign patterns per simulated test. Up to this many the test
#: enumerates every pattern; past it, a seeded draw of this many. At a p-value
#: near 0.025 the draw error is about 0.0025.
POWER_FLIPS = 4096
_SIMULATION_CHUNK = 256


@dataclass(frozen=True)
class DesignCase:
    """One held-out case as the frozen key fixes it."""

    kind: str
    group: str
    expects_memory: bool


@dataclass(frozen=True)
class DevCase:
    """One development case as measured: per arm, each repeat's score and Exomem calls."""

    kind: str
    expects_memory: bool
    repeats: Mapping[str, Sequence[float]]
    calls: Mapping[str, Sequence[float]]


def design_digest(design: Sequence[DesignCase]) -> str:
    """Digest of the design that rules 1 to 3 read: each miss case and ordinary
    turn's kind, group and memory flag, so the groups, their sizes and the
    strata, in no particular order."""

    rows = sorted(
        [c.kind, c.group, c.expects_memory] for c in design if c.kind in (KIND_MISS, KIND_ORDINARY)
    )
    return hashlib.sha256(json.dumps(rows).encode("utf-8")).hexdigest()


def repeat_spread(dev: Sequence[DevCase]) -> float:
    """Mean over cases and arms of the standard deviation between repeats."""

    spreads = [
        float(np.std(np.asarray(scores, dtype=float)))
        for case in dev
        for scores in case.repeats.values()
        if len(scores) > 1
    ]
    return float(np.mean(spreads)) if spreads else float("nan")


def _passes(sums: np.ndarray, *, rng: np.random.Generator, alpha: float) -> np.ndarray:
    """Per simulation (a row of group sums), whether the one-sided sign-flip test passes.

    The p-value is computed as ``SignFlip.p_value`` computes it, with fewer draws.
    """

    count = sums.shape[1]
    exact = 2**count <= POWER_FLIPS
    if exact:
        index = np.arange(2**count, dtype=np.int64)[:, None]
        signs = 1.0 - 2.0 * ((index >> np.arange(count, dtype=np.int64)[None, :]) & 1)
    else:
        signs = rng.integers(0, 2, size=(POWER_FLIPS, count)).astype(float) * 2.0 - 1.0
    result = np.empty(len(sums), dtype=bool)
    for start in range(0, len(sums), _SIMULATION_CHUNK):
        chunk = sums[start : start + _SIMULATION_CHUNK]
        observed = chunk.sum(axis=1, keepdims=True)
        hits = np.count_nonzero(chunk @ signs.T >= observed - _EPS, axis=1)
        p = hits / len(signs) if exact else (1 + hits) / (len(signs) + 1)
        result[start : start + _SIMULATION_CHUNK] = p <= alpha
    return result


def _resampled_means(vectors: np.ndarray, rows: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """For each chosen row of ``vectors``, the mean of a fresh set of repeats drawn from it."""

    draws = rng.integers(0, vectors.shape[1], size=(*rows.shape, protocol.REPEATS))
    return vectors[rows[..., None], draws].mean(axis=-1)


def _simulate(
    slots: Sequence[DesignCase],
    pools: Mapping[str, Sequence[DevCase]],
    *,
    compared: tuple[str, str],
    effects: Mapping[str, float],
    delta: float,
    seed: int,
    simulations: int,
    alpha: float,
    calls: bool = False,
) -> dict:
    """One simulated held-out experiment, repeated ``simulations`` times.

    Each slot draws a development case of its kind and one of that case's arms;
    both simulated arms then draw fresh repeats from that arm's measured
    repeats, so the arms are equal and each case keeps its own spread. The
    slot's effect for its kind is added to the difference, and the differences
    sum within the frozen groups. With ``calls``, the same development case
    also gives each compared arm its measured calls, and a simulation passes
    as rule 3 does: superiority, or non-inferiority within δ together with the
    calls test on the second arm's calls minus the first's. The
    non-inferiority and calls pass rates are recorded on their own as well.
    """

    rng = np.random.default_rng(seed)
    labels = sorted({slot.group for slot in slots})
    membership = np.zeros((len(slots), len(labels)))
    for index, slot in enumerate(slots):
        membership[index, labels.index(slot.group)] = 1.0
    quality = np.zeros((simulations, len(slots)))
    saved = np.zeros((simulations, len(slots)))
    for kind in sorted({slot.kind for slot in slots}):
        where = [index for index, slot in enumerate(slots) if slot.kind == kind]
        pool = pools[kind]
        arms = sorted(pool[0].repeats)
        scores = np.array([[case.repeats[arm] for arm in arms] for case in pool], dtype=float)
        sources = rng.integers(0, len(pool), size=(simulations, len(where)))
        vectors = scores.reshape(len(pool) * len(arms), -1)
        rows = sources * len(arms) + rng.integers(0, len(arms), size=sources.shape)
        quality[:, where] = (
            _resampled_means(vectors, rows, rng)
            - _resampled_means(vectors, rows, rng)
            + effects.get(kind, 0.0)
        )
        if calls:
            first, second = (
                np.array([case.calls[arm] for case in pool], dtype=float) for arm in compared
            )
            saved[:, where] = _resampled_means(second, sources, rng) - _resampled_means(
                first, sources, rng
            )
    sizes = membership.sum(axis=0)
    sums = quality @ membership
    within = _passes(sums + delta * sizes, rng=rng, alpha=alpha)
    diagnostics = {}
    passed = within
    if calls:
        cheaper = _passes(saved @ membership, rng=rng, alpha=alpha)
        passed = _passes(sums, rng=rng, alpha=alpha) | (within & cheaper)
        diagnostics = {
            "non_inferiority_pass_rate": float(within.mean()),
            "calls_pass_rate": float(cheaper.mean()),
        }
    return {
        "cases": len(slots),
        "groups": len(labels),
        "delta": delta or None,
        "pass_rate": float(passed.mean()),
        **diagnostics,
    }


#: Harness knob: the most further keyed turns the projection for a short rule searches.
PROJECTION_LIMIT = 256


def power_step(
    design: Sequence[DesignCase],
    dev: Sequence[DevCase],
    *,
    reserve: Sequence[DesignCase],
    seed: int,
    simulations: int = 2000,
    alpha: float = protocol.ALPHA_ONE_SIDED,
) -> dict:
    """The protocol's power step for rules 1 to 3, on the frozen held-out design.

    ``design`` is the held-out key's structure; ``reserve`` the keyed reserve
    turns' structure, in the reserve's fixed order; ``dev`` the development
    cases' measured repeats. Rule 1 simulates M beating A, each stratum at its
    own development M - A capped at +0.5; rules 2 and 3 simulate equal arms
    within δ, and rule 3 passes as the rule does, on superiority or on
    non-inferiority with fewer calls, so a calls test that cannot pass leaves
    it short. Reserve turns enter in order with their keyed group and memory
    flag, and each enters every rule, so the size the rules reach is the
    largest any rule needs; a reserve turn not yet keyed counts for nothing.
    At that size rules 2 and 3 raise δ from the floor to the cap; a rule below
    the target with every keyed reserve turn and at the cap is short.
    """

    design = [c for c in design if c.kind in (KIND_MISS, KIND_ORDINARY)]
    reserve = [c for c in reserve if c.kind in (KIND_MISS, KIND_ORDINARY)]
    dev = [c for c in dev if c.kind in (KIND_MISS, KIND_ORDINARY)]
    if any(set(c.repeats) != set(protocol.ARM_IDS) for c in dev):
        raise ValueError(f"every development case needs the repeats of arms {protocol.ARM_IDS}")
    pools = {kind: [c for c in dev if c.kind == kind] for kind in (KIND_MISS, KIND_ORDINARY)}
    effects = {}
    for kind, pool in pools.items():
        stratum = [c for c in pool if c.expects_memory]
        if stratum:
            measured = float(
                np.mean([np.mean(c.repeats["M"]) - np.mean(c.repeats["A"]) for c in stratum])
            )
            effects[kind] = {
                "measured": measured,
                "simulated": min(measured, protocol.RULE1_EFFECT_CAP),
            }

    def evaluate(rule: str, held: Sequence[DesignCase], delta: float) -> dict:
        if rule == "1":
            chosen = [c for c in held if c.expects_memory]
        elif rule == "2":
            chosen = [c for c in held if c.kind == KIND_ORDINARY]
        else:
            chosen = list(held)
        for kind in {c.kind for c in chosen}:
            if not pools[kind] or (rule == "1" and kind not in effects):
                raise ValueError(f"rule {rule}: no development {kind} cases to simulate from")
        return _simulate(
            chosen,
            pools,
            compared=("A", "M") if rule != "3" else ("M", "P"),
            effects={k: v["simulated"] for k, v in effects.items()} if rule == "1" else {},
            delta=delta,
            seed=seed,
            simulations=simulations,
            alpha=alpha,
            calls=rule == "3",
        )

    def with_reserve(additions: int) -> list[DesignCase]:
        return [*design, *reserve[:additions]]

    def further_turns(rule: str, delta: float) -> dict:
        """A lower bound on the keyed turns a short rule still needs past the
        keyed reserve: each further turn is taken as an ordinary turn in a group
        of its own that expects memory, the most a turn can add to any rule. It
        is a projection to plan a widening with, never a power figure."""

        if not pools[KIND_ORDINARY] or (rule == "1" and KIND_ORDINARY not in effects):
            return {"needed": None, "reason": "no development ordinary turns to project from"}
        base = with_reserve(len(reserve))
        taken = {c.group for c in base}
        candidates = (f"projected-{n}" for n in range(len(taken) + PROJECTION_LIMIT))
        fresh = [label for label in candidates if label not in taken][:PROJECTION_LIMIT]

        def reaches(count: int) -> bool:
            added = [DesignCase(KIND_ORDINARY, label, True) for label in fresh[:count]]
            return evaluate(rule, [*base, *added], delta)["pass_rate"] >= protocol.POWER_TARGET

        low, high = 0, 1
        while not reaches(high):
            if high == PROJECTION_LIMIT:
                return {"needed": None, "searched_up_to": PROJECTION_LIMIT}
            low, high = high, min(2 * high, PROJECTION_LIMIT)
        while high - low > 1:
            middle = (low + high) // 2
            low, high = (low, middle) if reaches(middle) else (middle, high)
        return {"needed": high, "lower_bound": True}

    floors = {"1": 0.0, "2": protocol.DELTA_FLOOR, "3": protocol.DELTA_FLOOR}
    tables: dict[str, list[dict]] = {rule: [] for rule in floors}
    needed = {}
    for rule, floor in floors.items():
        for additions in range(len(reserve) + 1):
            tables[rule].append(
                {"additions": additions, **evaluate(rule, with_reserve(additions), floor)}
            )
            if tables[rule][-1]["pass_rate"] >= protocol.POWER_TARGET:
                needed[rule] = additions
                break
    additions = max(needed.get(rule, len(reserve)) for rule in floors)
    rules = {}
    for rule, floor in floors.items():
        row = next((r for r in tables[rule] if r["additions"] == additions), None)
        if row is None:
            row = {"additions": additions, **evaluate(rule, with_reserve(additions), floor)}
            tables[rule].append(row)
        steps = round((protocol.DELTA_CAP - protocol.DELTA_FLOOR) / DELTA_STEP) if floor else 0
        for step in range(1, steps + 1):
            if row["pass_rate"] >= protocol.POWER_TARGET:
                break
            delta = round(floor + DELTA_STEP * step, 2)
            row = {"additions": additions, **evaluate(rule, with_reserve(additions), delta)}
            tables[rule].append(row)
        short = row["pass_rate"] < protocol.POWER_TARGET
        rules[rule] = {
            **row,
            "compares": "M - P" if rule == "3" else "M - A",
            "test": {
                "1": "superiority",
                "2": "non-inferiority",
                "3": "superiority, or non-inferiority with fewer calls",
            }[rule],
            "short": short,
            "more_keyed_turns": further_turns(rule, row["delta"] or 0.0)
            if short
            else {"needed": 0},
            "table": tables[rule],
        }
    rules["1"]["effects"] = effects
    return {
        "rules": rules,
        "additions": additions,
        "reserve": len(reserve),
        # What score --power checks before it applies these figures to a run.
        "design_digest": design_digest(with_reserve(additions)),
        "spread": repeat_spread(dev),
        "development_cases": {kind: len(pool) for kind, pool in pools.items()},
        "held_out": {
            kind: sum(1 for c in design if c.kind == kind) for kind in (KIND_MISS, KIND_ORDINARY)
        },
        "model": (
            "each slot draws a development case of its kind and one of its arms; both arms "
            "resample that arm's repeats; rule 1 adds its stratum's development M - A capped at "
            f"+{protocol.RULE1_EFFECT_CAP}; reserve turns enter in their fixed order with their "
            "keyed group and memory flag; rule 3 passes on superiority or on non-inferiority "
            "with fewer calls"
        ),
        "projection": (
            "for a short rule, a lower bound on further keyed turns past the keyed reserve, "
            "each taken as an ordinary turn in its own group that expects memory, searched up "
            f"to {PROJECTION_LIMIT}; not a power figure"
        ),
        "simulations": simulations,
        "seed": seed,
        "flips": POWER_FLIPS,
    }
