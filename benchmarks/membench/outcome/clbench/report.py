"""Report a CL-Bench run: per-arm scores, paired differences with intervals, and cost.

Every number is recomputed from the run's ``outcomes.jsonl`` files. A question
that an arm did not reach is reported as missing, never as a zero.

Usage::

    python -m membench.outcome.clbench.report --run-id r1-dev [--pairs F-N,E-F,E-N]
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path

from recall_parity.scoring import percentile_bootstrap_ci

from .run import RUN_ROOT

BOOTSTRAP_SEED = 20261107


def load_arm(arm_dir: Path) -> dict[str, dict]:
    rows = {}
    for line in (arm_dir / "outcomes.jsonl").read_text().splitlines():
        row = json.loads(line)
        rows[row["log"]["question_id"]] = row
    return rows


def arm_summary(rows: dict[str, dict]) -> dict:
    outcomes = [r["outcome"] for r in rows.values() if r["outcome"]]
    logs = [r["log"] for r in rows.values()]
    rewards = [o["reward"] for o in outcomes]
    usage: dict[str, int] = {}
    tools: dict[str, int] = {}
    for log in logs:
        for k, v in log["usage"].items():
            usage[k] = usage.get(k, 0) + v
        for k, v in log["tool_uses"].items():
            tools[k] = tools.get(k, 0) + v
    return {
        "questions": len(rows),
        "mean_reward": round(statistics.mean(rewards), 4) if rewards else None,
        "accuracy": round(sum(o["success"] for o in outcomes) / len(outcomes), 4)
        if outcomes
        else None,
        "mean_queries": round(statistics.mean(o["metadata"]["num_queries"] for o in outcomes), 2)
        if outcomes
        else None,
        "turns": sum(log["turns"] for log in logs),
        "seconds": round(sum(log["seconds"] for log in logs), 1),
        "timeouts": sum(log["timeouts"] for log in logs),
        "schema_repairs": sum(log["repairs"] for log in logs),
        "faults": sum(len(log["faults"]) for log in logs),
        "tokens": usage,
        # The CLI prices every call; on a subscription nothing is billed, so this is notional.
        "notional_cost_usd": round(sum(log["notional_cost_usd"] for log in logs), 2),
        "tool_uses": dict(sorted(tools.items())),
    }


def paired(a: dict[str, dict], b: dict[str, dict]) -> dict:
    shared = sorted(set(a) & set(b), key=lambda q: (len(q), q))
    diffs = [a[q]["outcome"]["reward"] - b[q]["outcome"]["reward"] for q in shared]
    low, high = percentile_bootstrap_ci(diffs, seed=BOOTSTRAP_SEED)
    return {
        "paired_questions": len(shared),
        "missing": sorted(set(a) ^ set(b)),
        "mean_difference": round(statistics.mean(diffs), 4),
        "ci95": [round(low, 4), round(high, 4)],
        "verdict": "no change" if low <= 0 <= high else ("better" if low > 0 else "worse"),
        "wins_losses_ties": [
            sum(d > 0 for d in diffs),
            sum(d < 0 for d in diffs),
            sum(d == 0 for d in diffs),
        ],
    }


def build(run_id: str, pairs: list[tuple[str, str]]) -> dict:
    run_dir = RUN_ROOT / "runs" / run_id
    arms = {
        p.name: load_arm(p) for p in sorted(run_dir.iterdir()) if (p / "outcomes.jsonl").exists()
    }
    return {
        "run_id": run_id,
        "interval": "percentile bootstrap over questions, 10,000 resamples, one run per arm",
        "arms": {name: arm_summary(rows) for name, rows in arms.items()},
        "pairs": {
            f"{a}-{b}": paired(arms[a], arms[b]) for a, b in pairs if a in arms and b in arms
        },
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--pairs", default="F-N,E-N,E-F")
    args = parser.parse_args(argv)
    pairs = [tuple(p.split("-", 1)) for p in args.pairs.split(",")]
    report = build(args.run_id, pairs)
    out = RUN_ROOT / "runs" / args.run_id / "report.json"
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
