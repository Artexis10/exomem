"""The sums and the one paired interval every outcome report uses.

A treatment and a baseline are compared over the same units (questions, matters, panel cases).
Each unit contributes one difference; the interval resamples units, so a unit that is several
sessions long counts once. Fewer than two paired units give no interval and a verdict that says so.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable

from recall_parity.scoring import percentile_bootstrap_ci

#: An interval over one unit is a point, not an interval.
MIN_UNITS = 2
#: The largest loss against the baseline the owner accepts (PREREGISTRATION section 7, sequence 9):
#: Exomem as shipped is not worse than a plain folder (f35, P - F) or than plain file search (f36, WP - S)
#: when the interval's lower bound is above it.
NOT_WORSE_BOUND = -0.05


def paired_interval(diffs: list[float], *, seed: int, unit: str, digits: int = 4) -> dict:
    """Mean difference, its 95% percentile bootstrap interval and a verdict.

    The verdict is ``better`` or ``worse`` when the interval excludes 0, ``no change`` when it
    contains 0, and ``insufficient`` below ``MIN_UNITS`` paired units, which is never ``better``.
    """
    entry: dict = {f"paired_{unit}s": len(diffs)}
    if len(diffs) < MIN_UNITS:
        return {
            **entry,
            "mean_difference": None,
            "ci95": None,
            "verdict": f"insufficient: {len(diffs)} paired {unit}(s), at least {MIN_UNITS} needed",
        }
    low, high = percentile_bootstrap_ci(diffs, seed=seed)
    return {
        **entry,
        "mean_difference": round(statistics.mean(diffs), digits),
        "ci95": [round(low, digits), round(high, digits)],
        "verdict": "no change" if low <= 0 <= high else ("better" if low > 0 else "worse"),
    }


def usage_totals(logs: Iterable[dict]) -> dict:
    """Turns, time, tokens by kind, cost and faults over session logs.

    A session whose CLI sent no ``result`` event reports no usage, so a sum over sessions that
    include one covers less than ran. The sums are then marked ``partial`` with the count of those
    sessions; they are never presented as the whole.
    """
    logs = list(logs)
    tokens: dict[str, int] = {}
    for log in logs:
        for kind, count in log["usage"].items():
            tokens[kind] = tokens.get(kind, 0) + count
    without_result = sum(1 for log in logs if not log.get("results"))
    return {
        "sessions": len(logs),
        "turns": sum(log["turns"] for log in logs),
        "seconds": round(sum(log["seconds"] for log in logs), 1),
        "tokens": tokens,
        # The CLI prices every call; on a subscription nothing is billed, so this is notional.
        "notional_cost_usd": round(sum(log["notional_cost_usd"] for log in logs), 2),
        "faults": sum(len(log["faults"]) for log in logs),
        "harness_faults": sum(len(log.get("harness_faults", [])) for log in logs),
        "sessions_without_result": without_result,
        "partial": without_result > 0,
    }
