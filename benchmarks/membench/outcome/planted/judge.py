"""Judge planted-error answers blind, then score the arms case by case.

``items`` pools every arm's answers, shuffles them under opaque ids and writes
the arm key to a separate file. ``judge`` gives one fresh judge process per
item the task, the case key and the answer: never the arm, the vault or the
recall packet. The judge returns a structured verdict. ``score`` joins verdicts
to the arm key and reports the paired difference per case with a bootstrap
interval. ``membench outcome report planted`` drives this module.
"""

from __future__ import annotations

import json
import random
import statistics
from pathlib import Path

from recall_parity.scoring import percentile_bootstrap_ci

from ..judging import Judge, JudgeSpec, grade_item, judge_all

SEED = 20261107
JUDGE_TIMEOUT_SECONDS = 600
MEASURES = ("stale_trusted", "correct", "flags_conflict", "rationalises")

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
        "value_used": {"type": "string", "enum": ["correct", "stale", "both", "neither"]},
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


def items(cases: list[dict], run: Path, judge_run: Path, arms: list[str]) -> None:
    """Pool every arm's answers under ``judge_run``. Pooling twice from unchanged answers is a no-op."""
    pool = []
    for case in cases:
        for arm in arms:
            answered = json.loads((run / arm / "answers" / f"{case['case_id']}.json").read_text())
            pool.append(
                {
                    "case_id": case["case_id"],
                    "arm": arm,
                    "task": case["task"],
                    "key": case["key"],
                    "answer": answered["answer"],
                }
            )
    random.Random(SEED).shuffle(pool)
    key, lines = {}, []
    for i, item in enumerate(pool):
        item_id = f"it-{i:04d}"
        key[item_id] = {"case_id": item["case_id"], "arm": item["arm"]}
        shown = {"id": item_id, "task": item["task"], "key": item["key"], "answer": item["answer"]}
        lines.append(json.dumps(shown) + "\n")
    items_file = judge_run / "judge" / "items.jsonl"
    if items_file.exists():
        if items_file.read_text() != "".join(lines):
            raise SystemExit(
                f"{items_file} was pooled from different answers; move {judge_run} aside to judge again"
            )
        return
    (judge_run / "judge").mkdir(parents=True)
    (judge_run / "unblinding").mkdir()
    items_file.write_text("".join(lines))
    # The arm key lives apart from the items; judge processes never receive it.
    (judge_run / "unblinding" / "arm-key.json").write_text(json.dumps(key, indent=2))
    print(f"{len(pool)} items", flush=True)


def build_prompt(item: dict) -> str:
    """Exactly what a judge reads for one item: the rubric, the task, the case key, the answer."""
    return (
        f"{RUBRIC}\n\nTASK:\n{item['task']}\n\nKEY:\n{json.dumps(item['key'], indent=1)}\n\n"
        f"ANSWER:\n{item['answer'] or '(no answer)'}"
    )


def judge(judge_run: Path, spec: JudgeSpec, rejudge_fraction: float, jobs: int) -> None:
    judge_dir = judge_run / "judge"
    items_in = [json.loads(line) for line in (judge_dir / "items.jsonl").read_text().splitlines()]

    def grade(item: dict, judge: Judge) -> dict:
        return grade_item(
            judge,
            item["id"],
            build_prompt(item),
            VERDICT_SCHEMA,
            out_dir=judge_dir,
            timeout=JUDGE_TIMEOUT_SECONDS,
            problem=lambda verdict: None,
        )

    judge_all(
        judge_dir, spec, items_in, grade, rejudge_fraction=rejudge_fraction, jobs=jobs, seed=SEED
    )


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def score(judge_run: Path, pairs: list[tuple[str, str]]) -> dict:
    key = json.loads((judge_run / "unblinding" / "arm-key.json").read_text())
    rows = read_rows(judge_run / "judge" / "verdicts.jsonl")
    by_case: dict[str, dict[str, dict]] = {}
    failed = []
    for row in rows:
        k = key[row["id"]]
        if row["verdict"] is None:
            failed.append(row["id"])
            continue
        by_case.setdefault(k["case_id"], {})[k["arm"]] = row["verdict"]
    report: dict = {
        "judge": json.loads((judge_run / "judge" / "judge.json").read_text()),
        "judge_model_reported": sorted(
            {m for r in rows for m in r.get("judge_model_reported") or []}
        ),
        "failed_judgements": failed,
        "arms": {
            arm: {"cases_judged": sum(arm in v for v in by_case.values())}
            for arm in sorted({k["arm"] for k in key.values()})
        },
        "pairs": {},
        "cases": dict(sorted(by_case.items())),
    }
    for a, b in pairs:
        measures = {}
        for measure in MEASURES:
            cases = sorted(c for c, arms in by_case.items() if a in arms and b in arms)
            diffs = [int(by_case[c][a][measure]) - int(by_case[c][b][measure]) for c in cases]
            if len(diffs) < 2:
                measures[measure] = {
                    "cases": len(cases),
                    "verdict": "unknown: too few paired cases",
                }
                continue
            low, high = percentile_bootstrap_ci(diffs, seed=SEED)
            measures[measure] = {
                "cases": len(cases),
                f"rate_{a}": round(statistics.mean(int(by_case[c][a][measure]) for c in cases), 3),
                f"rate_{b}": round(statistics.mean(int(by_case[c][b][measure]) for c in cases), 3),
                "mean_difference": round(statistics.mean(diffs), 3),
                "ci95": [round(low, 3), round(high, 3)],
                "verdict": "no change" if low <= 0 <= high else "differs",
            }
        report["pairs"][f"{a}-{b}"] = measures
    again = [r for r in read_rows(judge_run / "judge" / "rejudge.jsonl") if r["verdict"]]
    first = {r["id"]: r["verdict"] for r in rows if r["verdict"]}
    agree = [
        first[r["id"]]["stale_trusted"] == r["verdict"]["stale_trusted"]
        for r in again
        if r["id"] in first
    ]
    report["judge_self_agreement_stale_trusted"] = f"{sum(agree)}/{len(agree)}" if agree else None
    (judge_run / "score.json").write_text(json.dumps(report, indent=2))
    return report
