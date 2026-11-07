"""Report a CL-Bench run: per-arm scores, paired differences with intervals, and cost.

Every number is recomputed from the run's ``outcomes.jsonl`` files. A question
that an arm did not reach is reported as missing, never as a zero. An arm that
ran several ``run_index`` values is paired question by question inside each
``run_index``; the differences of one question are averaged before the interval,
so a repeated question counts once.

``membench outcome report clbench`` drives this module.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

from recall_parity.scoring import percentile_bootstrap_ci

BOOTSTRAP_SEED = 20261107
DEFAULT_PAIRS = ("F-N", "E-N", "E-F", "EH-E")


Rows = dict[tuple[int | None, str], dict]


def load_arms(run_dir: Path) -> dict[str, Rows]:
    """``arm -> {(run_index, question_id): row}``; the arm and run_index come from each manifest."""
    arms: dict[str, Rows] = defaultdict(dict)
    for cell in sorted(run_dir.iterdir()):
        if not (cell / "outcomes.jsonl").exists():
            continue
        manifest = json.loads((cell / "manifest.json").read_text())
        run_index = manifest["task"]["run_index"]
        for line in (cell / "outcomes.jsonl").read_text().splitlines():
            row = json.loads(line)
            arms[manifest["arm"]][(run_index, row["log"]["question_id"])] = row
    return dict(arms)


def arm_summary(rows: Rows) -> dict:
    outcomes = [r["outcome"] for r in rows.values() if r["outcome"]]
    logs = [r["log"] for r in rows.values()]
    rewards = [o["reward"] for o in outcomes]
    usage: dict[str, int] = {}
    tools: dict[str, int] = {}
    injected: dict[str, int] = {}
    for log in logs:
        for k, v in log["usage"].items():
            usage[k] = usage.get(k, 0) + v
        for k, v in log["tool_uses"].items():
            tools[k] = tools.get(k, 0) + v
        for turn in log.get("hook_chars", []):
            for event, chars in turn.items():
                injected[event] = injected.get(event, 0) + chars
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
        # Characters the shipped hook added to the context, by hook event; empty where no hook ran.
        "hook_chars": dict(sorted(injected.items())),
    }


def reached(rows: Rows) -> Rows:
    return {key: row for key, row in rows.items() if row["outcome"]}


def paired(a_rows: Rows, b_rows: Rows) -> dict:
    a, b = reached(a_rows), reached(b_rows)
    by_question: dict[str, list[float]] = defaultdict(list)
    for run_index, question in sorted(set(a) & set(b), key=lambda k: (k[0] or 0, len(k[1]), k[1])):
        by_question[question].append(
            a[(run_index, question)]["outcome"]["reward"]
            - b[(run_index, question)]["outcome"]["reward"]
        )
    shared = sorted(by_question, key=lambda q: (len(q), q))
    diffs = [statistics.mean(by_question[q]) for q in shared]
    if not diffs:
        return {"paired_questions": 0, "verdict": "unknown: no shared questions"}
    low, high = percentile_bootstrap_ci(diffs, seed=BOOTSTRAP_SEED)
    return {
        "paired_questions": len(shared),
        "missing": sorted(f"r{k[0]}:{k[1]}" if k[0] is not None else k[1] for k in set(a) ^ set(b)),
        "mean_difference": round(statistics.mean(diffs), 4),
        "ci95": [round(low, 4), round(high, 4)],
        "verdict": "no change" if low <= 0 <= high else ("better" if low > 0 else "worse"),
        "wins_losses_ties": [
            sum(d > 0 for d in diffs),
            sum(d < 0 for d in diffs),
            sum(d == 0 for d in diffs),
        ],
    }


def per_question(rows: Rows) -> dict[str, float]:
    """Each question's reward, averaged over the ``run_index`` values the arm played it in."""
    by_question: dict[str, list[float]] = defaultdict(list)
    for (_run_index, question), row in reached(rows).items():
        by_question[question].append(row["outcome"]["reward"])
    return {q: round(statistics.mean(v), 4) for q, v in sorted(by_question.items())}


def build(run_dir: Path, pairs: list[tuple[str, str]] | None = None) -> dict:
    arms = load_arms(run_dir)
    wanted = pairs or [tuple(p.split("-", 1)) for p in DEFAULT_PAIRS]
    cases: dict[str, dict[str, float]] = defaultdict(dict)
    for arm, rows in arms.items():
        for question, reward in per_question(rows).items():
            cases[question][arm] = reward
    return {
        "suite": "clbench",
        "run_id": run_dir.name,
        "measure": "mean reward per question (upstream scoring), 0 to 1",
        "interval": "percentile bootstrap over questions, 10,000 resamples",
        "arms": {name: arm_summary(rows) for name, rows in arms.items()},
        "pairs": {
            f"{a}-{b}": paired(arms[a], arms[b]) for a, b in wanted if a in arms and b in arms
        },
        "cases": dict(sorted(cases.items(), key=lambda kv: (len(kv[0]), kv[0]))),
    }
