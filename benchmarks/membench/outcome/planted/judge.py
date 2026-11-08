"""Judge planted-error answers blind, then score the arms case by case.

``items`` pools every arm's answered cases, shuffles them under opaque ids and writes
the arm key to a separate file. ``judge`` gives one fresh judge process per
item the task, the case key and the answer: never the arm, the vault or the
recall packet. The judge returns a structured verdict. ``score`` joins verdicts
to the arm key and reports the paired table of each measure with its Newcombe
interval (method 10, the sequence-8 implementation), after it checks that
implementation against that entry's test vector.

A case the actor left unanswered (no reply, a timeout, a crash of the actor) is not judged. It scores
as ``stale_trusted`` and not ``correct``, so an arm cannot gain by not answering, and it counts in
the arm's ``unanswered`` column. A case where any arm met a harness fault is struck from every arm;
more than ``STRIKE_LIMIT`` struck cases void the decision. ``membench outcome report planted``
drives this module.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

# ``_table`` is private to the sequence-8 scorer, whose module is outside this suite's scope; it is the
# one place that builds the (a, b, c, d) table that method 10 reads, so a copy here would drift from it.
from membench.utility.organisation_replay.score import _table, newcombe_paired

from ..faults import STRIKE_LIMIT, books, incomplete_why
from ..judging import Judge, JudgeSpec, failed_judgements, grade_item, judge_all, read_rows
from ..stats import MIN_UNITS, usage_totals

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


#: What an unanswered case scores without judging: it trusts the stale value and is not correct.
UNANSWERED_VERDICT = {
    "value_used": "neither",
    "stale_trusted": True,
    "correct": False,
    "flags_conflict": False,
    "rationalises": False,
    "reason": "unanswered",
}


def struck_cases(sessions: list[dict]) -> set[str]:
    """Cases where any arm met a harness fault: they are struck from every arm."""
    return {e["case_id"] for e in sessions if e["state"] == "harness_fault"}


def items(cases: list[dict], run: Path, judge_run: Path, arms: list[str]) -> None:
    """Pool every arm's answers under ``judge_run``. Pooling twice from unchanged answers is a no-op."""
    entries = []
    for case in cases:
        for arm in arms:
            entry = {"case_id": case["case_id"], "arm": arm, "item": None}
            answer_file = run / arm / "answers" / f"{case['case_id']}.json"
            if not answer_file.exists():
                entries.append({**entry, "state": "missing"})
                continue
            answered = json.loads(answer_file.read_text())
            entry["log"] = answered["log"]
            if answered["log"]["harness_faults"]:
                entry["state"] = "harness_fault"
            elif not (answered["answer"] or "").strip():
                entry["state"] = "unanswered"
            else:
                entry |= {"state": "answered", "task": case["task"], "key": case["key"]}
                entry["answer"] = answered["answer"]
            entries.append(entry)
    struck = struck_cases(entries)
    # A struck case counts for no arm, so its answers are not worth a judge call.
    pool = [e for e in entries if e["state"] == "answered" and e["case_id"] not in struck]
    random.Random(SEED).shuffle(pool)
    lines = []
    for i, entry in enumerate(pool):
        entry["item"] = f"it-{i:04d}"
        shown = {
            "id": entry["item"],
            "task": entry["task"],
            "key": entry["key"],
            "answer": entry["answer"],
        }
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
    kept = {"task", "key", "answer"}
    (judge_run / "unblinding" / "sessions.json").write_text(
        json.dumps([{k: v for k, v in e.items() if k not in kept} for e in entries], indent=2)
    )
    print(f"{len(pool)} items, {len(struck)} struck cases", flush=True)


def build_prompt(item: dict) -> str:
    """Exactly what a judge reads for one item: the rubric, the task, the case key, the answer."""
    return (
        f"{RUBRIC}\n\nTASK:\n{item['task']}\n\nKEY:\n{json.dumps(item['key'], indent=1)}\n\n"
        f"ANSWER:\n{item['answer'] or '(no answer)'}"
    )


def judge(judge_run: Path, spec: JudgeSpec, rejudge_fraction: float, jobs: int) -> None:
    judge_dir = judge_run / "judge"
    items_in = [json.loads(line) for line in (judge_dir / "items.jsonl").read_text().splitlines()]

    def grade(item: dict, judge: Judge, pass_name: str) -> dict:
        return grade_item(
            judge,
            item["id"],
            build_prompt(item),
            VERDICT_SCHEMA,
            out_dir=judge_dir,
            pass_name=pass_name,
            timeout=JUDGE_TIMEOUT_SECONDS,
            problem=lambda verdict: None,
        )

    judge_all(
        judge_dir, spec, items_in, grade, rejudge_fraction=rejudge_fraction, jobs=jobs, seed=SEED
    )


def require_newcombe_vector() -> None:
    """The sequence-8 implementation must reproduce that entry's test vector before anything is scored."""
    result = newcombe_paired(36, 12, 2, 0)
    got = (round(result["difference"], 4), round(result["lower"], 4), round(result["upper"], 4))
    if got != (0.2, 0.0569, 0.3404):
        raise RuntimeError(
            f"Newcombe method 10 gives {got} for (36, 12, 2, 0), not (0.2, 0.0569, 0.3404)"
        )


def pair_tables(
    by_case: dict[str, dict[str, dict]], candidate: str, baseline: str
) -> dict[str, dict]:
    """Per measure, the paired table of the cases both arms have, candidate minus baseline, unrounded."""
    rows = [
        {"candidate": arms[candidate], "baseline": arms[baseline]}
        for _case, arms in sorted(by_case.items())
        if candidate in arms and baseline in arms
    ]
    return {measure: _table(rows, measure) for measure in MEASURES}


def shown(table: dict) -> dict:
    """A table as the report prints it: the cells, the difference and its interval, rounded."""
    entry = {"cases": table["n"], **{cell: table[cell] for cell in "abcd"}}
    if table["n"] < MIN_UNITS:
        return {
            **entry,
            "mean_difference": None,
            "ci95": None,
            "verdict": f"insufficient: {table['n']} paired case(s), at least {MIN_UNITS} needed",
        }
    low, high = table["lower"], table["upper"]
    return {
        **entry,
        "mean_difference": round(table["difference"], 4),
        "ci95": [round(low, 4), round(high, 4)],
        # The difference is of a rate (candidate minus baseline), so "higher" is not "better" here.
        "verdict": "no change" if low <= 0 <= high else ("higher" if low > 0 else "lower"),
    }


def decision(
    tables: dict[str, dict] | None, struck: set[str], failed: list[str], arms: dict[str, dict]
) -> dict:
    """PREREGISTRATION §7: governance protects when the upper bound of E-P for stale_trusted is below 0.

    The point estimate of ``correct`` E-P must also be at least 0, so that an arm cannot gain by not answering.
    """
    if len(struck) > STRIKE_LIMIT:
        return {
            "status": "void",
            "why": f"{len(struck)} cases are struck by harness faults: {sorted(struck)}",
        }
    if why := incomplete_why(failed, arms):
        return {"status": "incomplete", "why": why}
    if tables is None or tables["stale_trusted"]["n"] < MIN_UNITS:
        return {"status": "incomplete", "why": ["no paired E-P cases"]}
    stale, correct = tables["stale_trusted"], tables["correct"]
    return {
        "status": "complete",
        "governance_protects": {
            "rule": "upper bound of E-P for stale_trusted below 0 and the point estimate of E-P for correct at least 0",
            "result": stale["upper"] < 0 and correct["difference"] >= 0,
        },
    }


def agreement(first: dict[str, dict], again: list[dict]) -> dict[str, str] | None:
    """Per measure, how many of the items both judgements settled got the same verdict, as ``k/n``; None when none did."""
    both = [(first[row["id"]], row["verdict"]) for row in again if row["id"] in first]
    if not both:
        return None
    return {m: f"{sum(a[m] == b[m] for a, b in both)}/{len(both)}" for m in MEASURES}


def sample_agreement(judge_run: Path, sample_run: Path) -> dict:
    """A second judge's verdicts on a sample, set against the first judge's: description only."""
    first = {
        r["id"]: r["verdict"]
        for r in read_rows(judge_run / "judge" / "verdicts.jsonl")
        if r["verdict"]
    }
    other = [r for r in read_rows(sample_run / "judge" / "verdicts.jsonl") if r["verdict"]]
    return {"items_judged": len(other), "agreement": agreement(first, other)}


def score(judge_run: Path, pairs: list[tuple[str, str]], *, heldout: bool = False) -> dict:
    require_newcombe_vector()
    sessions = json.loads((judge_run / "unblinding" / "sessions.json").read_text())
    by_item = {e["item"]: e for e in sessions if e["item"]}
    struck = struck_cases(sessions)
    rows = read_rows(judge_run / "judge" / "verdicts.jsonl")
    by_case: dict[str, dict[str, dict]] = {}
    failed = failed_judgements(judge_run / "judge")
    for row in rows:
        if row["verdict"] is not None:
            entry = by_item[row["id"]]
            by_case.setdefault(entry["case_id"], {})[entry["arm"]] = row["verdict"]
    unanswered = {
        (e["case_id"], e["arm"])
        for e in sessions
        if e["state"] == "unanswered" and e["case_id"] not in struck
    }
    for case, arm in unanswered:
        by_case.setdefault(case, {})[arm] = UNANSWERED_VERDICT
    for case in struck:
        by_case.pop(case, None)
    report: dict = {
        "judge": json.loads((judge_run / "judge" / "judge.json").read_text()),
        "judge_model_reported": sorted(
            {m for r in rows for m in r.get("judge_model_reported") or []}
        ),
        "failed_judgements": failed,
        "struck_units": sorted(struck),
        "arms": {},
        "pairs": {},
        "cases": dict(sorted(by_case.items())),
    }
    for arm in sorted({e["arm"] for e in sessions}):
        mine = [e for e in sessions if e["arm"] == arm]
        scheduled = {e["case_id"] for e in mine}
        valid = {c for c, arms in by_case.items() if arm in arms}
        report["arms"][arm] = {
            # Cases of this arm: valid (scored), invalid (struck by any arm's harness fault) or missing.
            "units": books(scheduled, valid, scheduled & struck),
            "cases_judged": len(valid) - sum(1 for c in valid if (c, arm) in unanswered),
            "unanswered": sum(1 for c in valid if (c, arm) in unanswered),
            "spent": usage_totals(e["log"] for e in mine if "log" in e),
        }
    raw = {}
    for a, b in pairs:
        raw[f"{a}-{b}"] = pair_tables(by_case, a, b)
        report["pairs"][f"{a}-{b}"] = {m: shown(t) for m, t in raw[f"{a}-{b}"].items()}
    if heldout:
        report["decision"] = decision(raw.get("E-P"), struck, failed, report["arms"])
    again = [r for r in read_rows(judge_run / "judge" / "rejudge.jsonl") if r["verdict"]]
    own = agreement({r["id"]: r["verdict"] for r in rows if r["verdict"]}, again)
    report["judge_self_agreement_stale_trusted"] = own["stale_trusted"] if own else None
    (judge_run / "score.json").write_text(json.dumps(report, indent=2))
    return report
