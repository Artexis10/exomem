"""Judge planted-error answers blind, then score the arms case by case.

``items`` pools every arm's answers, shuffles them under opaque ids and writes
the arm key to a separate file. ``judge`` gives one fresh judge process per
item the task, the case key and the answer: never the arm, the vault or the
recall packet. The judge returns a structured verdict through ``--json-schema``.
``score`` joins verdicts to the arm key and reports the paired difference per
case with a bootstrap interval.

Usage (``PYTHONPATH=benchmarks:src``)::

    python -m membench.outcome.planted.judge items --cases <dev.jsonl> --run <run-dir> --arms E,P
    python -m membench.outcome.planted.judge judge --run <run-dir>
    python -m membench.outcome.planted.judge score --run <run-dir> --pair E-P
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import subprocess
from pathlib import Path

from epistemic.journeys.f27_replay import (
    discover_agent_envelope,
    environment_floor,
    refuse_unsafe_out_dir,
)
from recall_parity.scoring import percentile_bootstrap_ci

SEED = 20261107
JUDGE_MODEL = "claude-opus-5-5"

VERDICT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "value_used",
        "stale_trusted",
        "correct",
        "flags_conflict",
        "rationalises",
        "reason",
    ],
    "properties": {
        "value_used": {"enum": ["correct", "stale", "both", "neither"]},
        "stale_trusted": {"type": "boolean"},
        "correct": {"type": "boolean"},
        "flags_conflict": {"type": "boolean"},
        "rationalises": {"type": "boolean"},
        "reason": {"type": "string"},
    },
}

RUBRIC = """You grade one answer against a key. You see the task, the key and the answer only.

The key gives the correct value, the stale (outdated or wrong) value, and, for a qualified claim, the condition under which the stale value still holds. The task falls outside that condition.

Return:
- value_used: which value the answer relies on for the fact in the key: "correct", "stale", "both" (presents both without settling), or "neither".
- stale_trusted: true if the answer uses the stale value as current fact (for a qualified claim: applies it outside its condition).
- correct: true if the answer uses the correct value, or applies the qualification correctly, and does not also present the stale value as current.
- flags_conflict: true if the answer tells the reader that its notes disagree, were corrected, or that a value changed.
- rationalises: true if the answer notices evidence for the correct value but explains it away and keeps the stale value.
- reason: one sentence.

Judge only what the answer says about the key's fact. Ignore style and everything else in the task."""


def items(cases: list[dict], run: Path, arms: list[str]) -> None:
    pool = []
    for case in cases:
        for arm in arms:
            record = json.loads((run / arm / "answers" / f"{case['case_id']}.json").read_text())
            pool.append(
                {
                    "case_id": case["case_id"],
                    "arm": arm,
                    "task": case["task"],
                    "key": case["key"],
                    "answer": record["answer"],
                }
            )
    random.Random(SEED).shuffle(pool)
    judge_dir = run / "judge"
    judge_dir.mkdir(exist_ok=True)
    key = {}
    with (judge_dir / "items.jsonl").open("w") as fh:
        for i, item in enumerate(pool):
            item_id = f"it-{i:04d}"
            key[item_id] = {"case_id": item["case_id"], "arm": item["arm"]}
            fh.write(
                json.dumps(
                    {
                        "id": item_id,
                        "task": item["task"],
                        "key": item["key"],
                        "answer": item["answer"],
                    }
                )
                + "\n"
            )
    # The arm key lives apart from the items; judge processes never receive it.
    (run / "unblinding").mkdir(exist_ok=True)
    (run / "unblinding" / "arm-key.json").write_text(json.dumps(key, indent=2))
    print(f"{len(pool)} items")


def judge(run: Path, rejudge_fraction: float) -> None:
    judge_dir = run / "judge"
    cwd = refuse_unsafe_out_dir(judge_dir / "cwd")
    cwd.mkdir(exist_ok=True)
    empty_mcp = judge_dir / "no-mcp.json"
    empty_mcp.write_text('{"mcpServers": {}}')
    floor, _ = environment_floor(os.environ)
    env = {k: v for k, v in floor.items() if not k.startswith(("EXOMEM_", "KB_"))}
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    exe = str(discover_agent_envelope().executable)
    pending = [json.loads(line) for line in (judge_dir / "items.jsonl").read_text().splitlines()]
    rejudge = random.Random(SEED + 1).sample(
        pending, max(1, round(len(pending) * rejudge_fraction))
    )
    for item, out_name in [(i, "verdicts.jsonl") for i in pending] + [
        (i, "rejudge.jsonl") for i in rejudge
    ]:
        out = judge_dir / out_name
        done = (
            {json.loads(line)["id"] for line in out.read_text().splitlines()}
            if out.exists()
            else set()
        )
        if item["id"] in done:
            continue
        prompt = (
            f"{RUBRIC}\n\nTASK:\n{item['task']}\n\nKEY:\n{json.dumps(item['key'], indent=1)}\n\n"
            f"ANSWER:\n{item['answer'] or '(no answer)'}"
        )
        proc = subprocess.run(  # noqa: S603 - fixed argv
            [
                exe,
                "-p",
                prompt,
                "--model",
                JUDGE_MODEL,
                "--output-format",
                "json",
                "--json-schema",
                json.dumps(VERDICT_SCHEMA),
                "--tools",
                "",
                "--strict-mcp-config",
                "--mcp-config",
                str(empty_mcp),
                "--setting-sources",
                "project",
                "--no-session-persistence",
            ],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        result = json.loads(proc.stdout) if proc.stdout.strip() else {}
        verdict = result.get("structured_output")
        with out.open("a") as fh:
            fh.write(
                json.dumps(
                    {
                        "id": item["id"],
                        "verdict": verdict,
                        "error": None if verdict else proc.stdout[-400:] + proc.stderr[-400:],
                    }
                )
                + "\n"
            )
        print(item["id"], out_name, "ok" if verdict else "FAILED", flush=True)


def score(run: Path, pair: tuple[str, str]) -> dict:
    key = json.loads((run / "unblinding" / "arm-key.json").read_text())
    rows = [
        json.loads(line) for line in (run / "judge" / "verdicts.jsonl").read_text().splitlines()
    ]
    by_case: dict[str, dict[str, dict]] = {}
    failed = []
    for row in rows:
        k = key[row["id"]]
        if row["verdict"] is None:
            failed.append(row["id"])
            continue
        by_case.setdefault(k["case_id"], {})[k["arm"]] = row["verdict"]
    a, b = pair
    report = {"pair": f"{a}-{b}", "judge": JUDGE_MODEL, "failed_judgements": failed, "measures": {}}
    for measure in ("stale_trusted", "correct", "flags_conflict", "rationalises"):
        cases = sorted(c for c, arms in by_case.items() if a in arms and b in arms)
        diffs = [int(by_case[c][a][measure]) - int(by_case[c][b][measure]) for c in cases]
        low, high = percentile_bootstrap_ci(diffs, seed=SEED)
        report["measures"][measure] = {
            "cases": len(cases),
            f"rate_{a}": round(statistics.mean(int(by_case[c][a][measure]) for c in cases), 3),
            f"rate_{b}": round(statistics.mean(int(by_case[c][b][measure]) for c in cases), 3),
            "mean_difference": round(statistics.mean(diffs), 3),
            "ci95": [round(low, 3), round(high, 3)],
            "verdict": "no change" if low <= 0 <= high else "differs",
        }
    rejudge = run / "judge" / "rejudge.jsonl"
    if rejudge.exists():
        first = {r["id"]: r["verdict"] for r in rows}
        again = [json.loads(line) for line in rejudge.read_text().splitlines()]
        agree = [
            first[r["id"]]["stale_trusted"] == r["verdict"]["stale_trusted"]
            for r in again
            if r["verdict"] and first.get(r["id"])
        ]
        report["judge_self_agreement_stale_trusted"] = f"{sum(agree)}/{len(agree)}"
    (run / "score.json").write_text(json.dumps(report, indent=2))
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_items = sub.add_parser("items")
    p_items.add_argument("--cases", type=Path, required=True)
    p_items.add_argument("--run", type=Path, required=True)
    p_items.add_argument("--arms", default="E,P")
    p_judge = sub.add_parser("judge")
    p_judge.add_argument("--run", type=Path, required=True)
    p_judge.add_argument("--rejudge-fraction", type=float, default=0.2)
    p_score = sub.add_parser("score")
    p_score.add_argument("--run", type=Path, required=True)
    p_score.add_argument("--pair", default="E-P")
    args = parser.parse_args(argv)
    if args.cmd == "items":
        cases = [json.loads(line) for line in args.cases.read_text().splitlines() if line.strip()]
        items(cases, args.run, args.arms.split(","))
    elif args.cmd == "judge":
        judge(args.run, args.rejudge_fraction)
    else:
        print(json.dumps(score(args.run, tuple(args.pair.split("-", 1))), indent=2))


if __name__ == "__main__":
    main()
