"""Scoring package: deterministic gates first, judge (optional) never above."""

from __future__ import annotations

from collections import defaultdict

from membench.scoring.gates import (
    ALL_GATES,
    GateStatus,
    ScoreItem,
    ScoringContext,
    evaluate,
)

__all__ = [
    "ALL_GATES",
    "GateStatus",
    "ScoreItem",
    "ScoringContext",
    "evaluate",
    "summarize_dimensions",
]


def summarize_dimensions(
    per_query_items: list[list[ScoreItem]], run_failures: int
) -> dict[str, dict[str, int]]:
    """Per-dimension tallies; run failures stay visible in the summary."""

    summary: dict[str, dict[str, int]] = defaultdict(
        lambda: {"pass": 0, "fail": 0, "not_applicable": 0, "unsupported": 0}
    )
    for items in per_query_items:
        for item in items:
            summary[item.dimension][item.status.value] += 1
    result = {dim: dict(counts) for dim, counts in sorted(summary.items())}
    result["_run"] = {"failures": run_failures, "queries_scored": len(per_query_items)}
    return result
