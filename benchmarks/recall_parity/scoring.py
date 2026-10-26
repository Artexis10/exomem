"""Score a captured run against judgements: paired nDCG@10, bootstrap CI, latency.

Judgements are graded relevance 0, 1 or 2 per pair id. The arm key maps pair
ids back to a (case, document), so each arm's ranking is read from its own
capture and graded from the pooled judgements for that case. nDCG uses the
product's existing ``exomem.eval_metrics.ndcg_at_k`` (exponential gain,
``2**grade - 1``); its ideal ranking is the best of the whole judged pool.

What is excluded, and what is not, is reported and never absorbed:

- a case with any returned pair unjudged is excluded (``unjudged``), because
  its nDCG cannot be computed exactly;
- a case whose pool has no relevant document is excluded
  (``no_relevant_in_pool``), because nDCG is undefined there;
- an arm that errored on a case scores nDCG 0 for it in the headline and the
  case is counted under ``arm_errors``; a second statistic leaves those cases
  out, so the cost of errors is visible as the gap between the two.

The pass rule is applied by the reader of the report, not coded here.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from exomem.eval_metrics import ndcg_at_k

from . import private
from .capture import capture_path

SCHEMA = "recall-parity/score/1"
GRADES = (0, 1, 2)
DEFAULT_RESAMPLES = 10_000
_CHUNK = 1_000


def percentile_bootstrap_ci(
    values: list[float], *, seed: int, resamples: int = DEFAULT_RESAMPLES, confidence: float = 0.95
) -> tuple[float, float]:
    """Percentile bootstrap interval of the mean, resampling cases with replacement."""

    data = np.asarray(values, dtype=float)
    if data.size == 0:
        raise ValueError("a bootstrap needs at least one value")
    rng = np.random.default_rng(seed)
    means = np.concatenate(
        [
            data[rng.integers(0, data.size, size=(min(_CHUNK, resamples - start), data.size))].mean(
                axis=1
            )
            for start in range(0, resamples, _CHUNK)
        ]
    )
    tail = (1 - confidence) / 2 * 100
    low, high = np.percentile(means, [tail, 100 - tail])
    return float(low), float(high)


def latency_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Client-side latency over the calls that succeeded; errors are counted apart."""

    ok = [row["latency_ms"] for row in rows if row["ok"]]
    kinds = Counter(row["error"]["kind"] for row in rows if not row["ok"])
    summary: dict[str, Any] = {
        "calls": len(rows),
        "ok": len(ok),
        "errors": dict(sorted(kinds.items())),
        "scope": "wall-clock at the client around the tool call; successful calls only",
        "percentile_method": "linear interpolation",
        "p50_ms": None,
        "p95_ms": None,
    }
    if ok:
        p50, p95 = np.percentile(ok, [50, 95])
        summary["p50_ms"], summary["p95_ms"] = round(float(p50), 3), round(float(p95), 3)
    return summary


def read_grades(path: Path, key: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    """Valid grades by pair id, and counts of the rows that were not usable.

    A row is unusable when it is not an object with a known ``pair_id`` and a
    grade in 0, 1, 2. A pair graded twice with different grades is unusable
    too; its grade is dropped, so the pair counts as unjudged.
    """

    seen: dict[str, set[int]] = {}
    stats = Counter(rows=0, malformed=0, unknown_pair=0)
    for row in private.read_jsonl(path):
        stats["rows"] += 1
        grade = row.get("grade") if isinstance(row, dict) else None
        identifier = row.get("pair_id") if isinstance(row, dict) else None
        if not isinstance(grade, int) or isinstance(grade, bool) or grade not in GRADES:
            stats["malformed"] += 1
        elif identifier not in key["pairs"]:
            stats["unknown_pair"] += 1
        else:
            seen.setdefault(identifier, set()).add(grade)
    conflicting = {identifier for identifier, grades in seen.items() if len(grades) > 1}
    grades = {i: next(iter(g)) for i, g in seen.items() if i not in conflicting}
    return grades, {**stats, "conflicting_pairs": len(conflicting)}


def _statistics(
    rows: list[dict[str, Any]], baseline: str, candidate: str, seed: int, resamples: int
) -> dict[str, Any]:
    if not rows:
        return {"cases": 0, "mean": None, "mean_difference": None, "ci95": None}
    differences = [row["difference"] for row in rows]
    low, high = percentile_bootstrap_ci(differences, seed=seed, resamples=resamples)
    return {
        "cases": len(rows),
        "mean": {
            baseline: float(np.mean([row["ndcg"][baseline] for row in rows])),
            candidate: float(np.mean([row["ndcg"][candidate] for row in rows])),
        },
        "mean_difference": float(np.mean(differences)),
        "ci95": [low, high],
    }


def score_run(
    root: Path,
    run_name: str,
    judgments: Path,
    *,
    baseline: str,
    candidate: str,
    resamples: int = DEFAULT_RESAMPLES,
) -> dict[str, Any]:
    """Score one run; the difference is ``candidate`` minus ``baseline``."""

    run_dir = root / "runs" / run_name
    manifest = private.read_json(run_dir / "manifest.json")
    key = private.read_json(run_dir / "key" / "arm-key.json")
    if {baseline, candidate} != set(key["arms"]) or baseline == candidate:
        raise ValueError(f"--baseline and --candidate must name the two arms {key['arms']}")
    k, seed = key["k"], key["seed"]
    grades, judged_stats = read_grades(judgments, key)
    captured = {
        arm: {row["case_id"]: row for row in private.read_jsonl(capture_path(run_dir, arm))}
        for arm in key["arms"]
    }

    pool: dict[str, dict[str, int]] = {}
    unjudged_pairs = 0
    for identifier, pair in key["pairs"].items():
        if identifier in grades:
            pool.setdefault(pair["case_id"], {})[pair["id"]] = grades[identifier]
        else:
            unjudged_pairs += 1

    scored: list[dict[str, Any]] = []
    excluded: Counter[str] = Counter()
    for case in manifest["case_ids"]:
        rows = {arm: captured[arm].get(case) for arm in key["arms"]}
        ranked = {
            arm: [r["id"] for r in row["results"][:k]] if row and row["ok"] else []
            for arm, row in rows.items()
        }
        judged = pool.get(case, {})
        if any(document not in judged for ids in ranked.values() for document in ids):
            excluded["unjudged"] += 1
        elif max(judged.values(), default=0) == 0:
            excluded["no_relevant_in_pool"] += 1
        else:
            errored = sorted(arm for arm, row in rows.items() if row is None or not row["ok"])
            score = {arm: ndcg_at_k(ranked[arm], judged, k) for arm in key["arms"]}
            scored.append(
                {
                    "case_id": case,
                    "ndcg": score,
                    "difference": score[candidate] - score[baseline],
                    "errored_arms": errored,
                }
            )

    all_rows = {
        arm: [row for case in manifest["case_ids"] if (row := captured[arm].get(case))]
        for arm in key["arms"]
    }
    return {
        "schema": SCHEMA,
        "run": run_name,
        "metric": f"nDCG@{k}, exponential gain 2**grade - 1, grades {list(GRADES)}",
        "difference": f"{candidate} minus {baseline}",
        "bootstrap": {
            "method": "percentile, cases resampled with replacement",
            "resamples": resamples,
            "seed": seed,
            "confidence": 0.95,
        },
        "coverage": {
            "cases": len(manifest["case_ids"]),
            "scored": len(scored),
            "excluded": dict(sorted(excluded.items())),
            "cases_with_arm_error": sum(bool(row["errored_arms"]) for row in scored),
            "pairs": len(key["pairs"]),
            "pairs_unjudged": unjudged_pairs,
            "judgement_rows": judged_stats,
        },
        "headline": {
            "scope": "scored cases; an arm that errored counts as nDCG 0",
            **_statistics(scored, baseline, candidate, seed, resamples),
        },
        "excluding_cases_with_arm_error": _statistics(
            [row for row in scored if not row["errored_arms"]], baseline, candidate, seed, resamples
        ),
        "latency": {arm: latency_summary(rows) for arm, rows in all_rows.items()},
        "per_case": scored,
        "judgements_sha256": hashlib.sha256(judgments.read_bytes()).hexdigest(),
    }
