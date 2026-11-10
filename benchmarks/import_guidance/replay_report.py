"""The replay report: for each set of cases, the paired difference between two arms on stage-B answers.

An answer passes when every named judged criterion holds in the blind batch (`judge_batch.py`) and every deterministic
check of the case passes. A run error, a missing judgment, an unreadable one, a missing arm and a run in which the agent
used a tool outside the script are `unknown`: they are counted and listed, and never read as a failure or as zero. A
case's value is the mean over the models that ran it of each model's mean over its known repeats, so a model with an
extra repeat weighs no more than one without. The interval is a 95% percentile bootstrap over cases
(`store_report.paired_interval`). Reply length is the characters of the final answer, compared as a per-case log ratio
with the same weighting.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path

from . import checks
from .judge_batch import agreement_line
from .replay_cases import SCRIPT_CHECK
from .report import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED, read_rows
from .store_report import paired_interval

Value = Callable[[dict], float | None]


def run_key(row: dict) -> tuple:
    return row["scenario"], row["arm"], row["model"], row["repeat"]


def used_outside_script(row: dict) -> bool:
    """Whether the agent used a tool that the scenario did not serve or allow. Such a run did not measure the replayed
    context, so no difference between the arms may rest on it."""
    return any(o["check"] == SCRIPT_CHECK and o["verdict"] == checks.FAIL for o in row["checks"])


def judged_outcomes(judged: dict | None, criteria: Sequence[str] | None) -> list[checks.Outcome]:
    """The judged criteria that count for one run: those named, or with `criteria` None every criterion that the run's
    judgment holds (the rubric its scenario carried). A missing judgment, or one that holds no criterion, is unknown."""
    found = {o["check"]: o for o in (judged or {}).get("criteria", [])}
    names = list(found) if criteria is None else criteria
    if judged is None or not names:
        return [checks.Outcome("judgment", checks.UNKNOWN, "no judgment")]
    return [checks.Outcome(**found[name]) if name in found else checks.Outcome(name, checks.UNKNOWN, "no judgment") for name in names]


def verdict_of(row: dict, judged: dict | None, criteria: Sequence[str] | None) -> str:
    """Pass, fail or unknown for one run, from its deterministic checks and the counting criteria of its judgment."""
    if row["error"] is not None or used_outside_script(row):
        return checks.UNKNOWN
    return checks.verdict(None, [checks.Outcome(**o) for o in row["checks"]], judged_outcomes(judged, criteria))


Pairs = dict[str, dict[str, list[tuple[float, float]]]]


def known_pairs(rows: Sequence[dict], baseline: str, candidate: str, value: Value) -> tuple[Pairs, int]:
    """Per case and model, the (baseline, candidate) values of each repeat where both arms have one, and how many
    (case, model, repeat) clusters lack one on either arm."""
    clusters: dict[tuple, dict[str, float | None]] = defaultdict(dict)
    for row in rows:
        clusters[(row["scenario"], row["model"], row["repeat"])][row["arm"]] = value(row)
    per_case: Pairs = defaultdict(lambda: defaultdict(list))
    unknown = 0
    for (case, model, _repeat), values in clusters.items():
        pair = (values.get(baseline), values.get(candidate))
        if None in pair:
            unknown += 1
        else:
            per_case[case][model].append(pair)
    return per_case, unknown


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def case_value(by_model: dict[str, list[tuple[float, float]]]) -> tuple[float, float]:
    """A case's (baseline, candidate) value: each model's mean over its known repeats, then the mean over models."""
    per_model = [(mean([b for b, _ in pairs]), mean([c for _, c in pairs])) for pairs in by_model.values()]
    return mean([b for b, _ in per_model]), mean([c for _, c in per_model])


def difference(per_case: Pairs, cases: Sequence[str], unknown_pairs: int, *, log: bool = False) -> str:
    """One sentence: the mean per-case difference with its interval, the cases it rests on and the unknown ones."""
    means = {case: case_value(by_model) for case, by_model in per_case.items() if case in cases and by_model}
    missing = [case for case in cases if case not in means]
    rest = f"unknown cases ({len(missing)}): {', '.join(missing) or 'none'}; unknown (case, model, repeat) pairs: {unknown_pairs}"
    if not means:
        return f"no paired case of {len(cases)}; {rest}."
    low, high = paired_interval(list(means.values()))
    average = mean([c - b for b, c in means.values()])
    verdict = "no change" if low <= 0 <= high else "interval excludes zero"
    shown = (
        f"mean log ratio {average:+.3f} (candidate over baseline x{math.exp(average):.2f}; 95% interval {low:+.3f} to {high:+.3f}; {verdict})"
        if log
        else f"difference {average:+.2f} (95% interval {low:+.2f} to {high:+.2f}; {verdict})"
    )
    return f"{shown} over {len(means)} paired case(s) of {len(cases)}; {rest}."


def check_verdict(row: dict) -> str | None:
    """The deterministic checks alone: fail if any fails, pass if all pass, None when there is none or a gap."""
    states = {o["verdict"] for o in row["checks"] if o["verdict"] != checks.NOT_APPLICABLE}
    return checks.FAIL if checks.FAIL in states else checks.PASS if states == {checks.PASS} else None


def judge_verdict(judged: dict | None, criteria: Sequence[str] | None) -> str | None:
    """The counting criteria alone: fail if any fails, pass if all pass, None when a judgment is missing or partial."""
    states = {o.verdict for o in judged_outcomes(judged, criteria)}
    return checks.FAIL if checks.FAIL in states else checks.PASS if states == {checks.PASS} else None


def hook_counts(rows: Sequence[dict]) -> str:
    seen = [r.get("hook_invocations") for r in rows]
    return f"{sum((n or 0) > 0 for n in seen)} with, {sum(n == 0 for n in seen)} without, {sum(n is None for n in seen)} unknown, of {len(rows)} runs"


def build(
    runs: Sequence[Path], *, judged: Path | None, case_sets: Path, baseline: str, candidate: str, criteria: Sequence[str] | None = None
) -> str:
    """The report. `criteria` names the judged criteria that must hold; None counts every criterion of each judgment."""
    sets_of = json.loads(case_sets.read_text(encoding="utf-8"))
    rows = [r for r in read_rows(runs)[0] if r.get("arm") in (baseline, candidate)]
    outside = sorted({r["scenario"] for r in rows} - set(sets_of))
    if outside:
        raise SystemExit(f"cases {outside} are in the runs but not in the case-set map {case_sets}")
    agreement, judged_rows, asked = (None, {}, [])
    if judged is not None:
        data = json.loads(judged.read_text(encoding="utf-8"))
        agreement = data["agreement"]
        judged_rows = {run_key(r): r for r in data["rows"] if r["stage"] == "B"}
        asked = data.get("criteria") or []
    verdicts = {run_key(r): verdict_of(r, judged_rows.get(run_key(r)), criteria) for r in rows}

    def passes(row: dict) -> float | None:
        return {checks.PASS: 1.0, checks.FAIL: 0.0}.get(verdicts[run_key(row)])

    def length(row: dict) -> float | None:
        measured = row["error"] is None and not used_outside_script(row)
        return math.log(len(row["final_text"])) if measured and row.get("final_text") else None

    shas = {r["arm"]: r.get("product_sha") for r in rows}
    models = sorted({r["model"] for r in rows})
    sets = sorted(set(sets_of.values()))
    in_set = {name: sorted(case for case, s in sets_of.items() if s == name) for name in sets}
    out = [
        f"Baseline `{baseline}` at `{shas.get(baseline)}`. Candidate `{candidate}` at `{shas.get(candidate)}`.",
        f"Stage-B runs read: {len(rows)}, of which {sum(r['error'] is not None for r in rows)} ended in an error (unknown). "
        "Counting criteria: "
        + (", ".join(criteria) if criteria is not None else f"every criterion of each judgment ({', '.join(asked) or 'no judge batch given'})")
        + f" and every deterministic check of the case. Models: {', '.join(models)}.",
        "Runs where the agent used a tool outside the script (unknown for every difference below): "
        + f"{sum(map(used_outside_script, rows))} of {len(rows)} ("
        + ", ".join(f"{arm} {sum(used_outside_script(r) for r in rows if r['arm'] == arm)}" for arm in (baseline, candidate))
        + ").",
        (agreement_line(agreement) if agreement else "No judge batch was given: every judged criterion is unknown.")
        + ("" if agreement and not agreement["incomplete"] else " The result below is incomplete."),
        "Runs whose stream shows a hook response (information only, not a gate): "
        + "; ".join(f"{arm} {hook_counts([r for r in rows if r['arm'] == arm])}" for arm in (baseline, candidate))
        + ".",
        "",
        "Differences are candidate minus baseline. A case's value is each model's mean over its known repeats, averaged "
        f"over the models; the interval is a 95% percentile bootstrap over cases ({BOOTSTRAP_RESAMPLES} resamples, seed "
        f"{BOOTSTRAP_SEED}). "
        "An interval that includes zero reads no change.",
        "",
        "## Correctness by set",
        "",
        "| set | arm | pass | known | unknown runs |",
        "|---|---|---|---|---|",
    ]
    for name in sets:
        for arm in (baseline, candidate):
            mine = [verdicts[run_key(r)] for r in rows if r["arm"] == arm and sets_of[r["scenario"]] == name]
            out.append(f"| {name} | {arm} | {mine.count(checks.PASS)} | {mine.count(checks.PASS) + mine.count(checks.FAIL)} | {mine.count(checks.UNKNOWN)} |")
    out.append("")
    for name in sets:
        mine = [r for r in rows if sets_of[r["scenario"]] == name]
        per_case, unknown = known_pairs(mine, baseline, candidate, passes)
        out.append(f"- Set {name}, all models: {difference(per_case, in_set[name], unknown)}")
    out += ["", "## Reply length (characters of the final answer)", ""]
    for name in sets:
        mine = [r for r in rows if sets_of[r["scenario"]] == name]
        per_case, unknown = known_pairs(mine, baseline, candidate, length)
        out.append(f"- Set {name}, all models: {difference(per_case, in_set[name], unknown, log=True)}")
    out += ["", "## Per model (report only; the decision reads all models together)", ""]
    for name in sets:
        for model in models:
            mine = [r for r in rows if sets_of[r["scenario"]] == name and r["model"] == model]
            per_case, unknown = known_pairs(mine, baseline, candidate, passes)
            out.append(f"- Set {name}, {model}: {difference(per_case, in_set[name], unknown)}")
    out += ["", "## Per case", "", "| case | set | baseline pass/known | candidate pass/known | difference |", "|---|---|---|---|---|"]
    for case in sorted(sets_of):
        mine = [r for r in rows if r["scenario"] == case]
        counts = []
        for arm in (baseline, candidate):
            found = [verdicts[run_key(r)] for r in mine if r["arm"] == arm]
            counts.append(f"{found.count(checks.PASS)}/{found.count(checks.PASS) + found.count(checks.FAIL)}")
        by_model = known_pairs(mine, baseline, candidate, passes)[0].get(case)
        shown = "unknown"
        if by_model:
            base_value, cand_value = case_value(by_model)
            shown = f"{cand_value - base_value:+.2f}"
        out.append(f"| {case} | {sets_of[case]} | {counts[0]} | {counts[1]} | {shown} |")
    split = [
        r
        for r in rows
        if r["error"] is None
        and not used_outside_script(r)
        and {s for s in (check_verdict(r), judge_verdict(judged_rows.get(run_key(r)), criteria)) if s} == {checks.PASS, checks.FAIL}
    ]
    out += ["", "## Deterministic checks and the judge disagree", ""]
    out += [f"- {r['arm']} {r['model']} {r['scenario']}#{r['repeat']}" for r in split] or ["None."]
    unusable = [r for r in rows if verdicts[run_key(r)] == checks.UNKNOWN]
    out += ["", "## Runs without a usable result", ""]
    out += [
        f"- {r['arm']} {r['model']} {r['scenario']}#{r['repeat']}: "
        + (r["error"] or ("the agent used a tool outside the script" if used_outside_script(r) else "a judgment or a check is missing"))[:160]
        for r in unusable
    ] or ["None."]
    return "\n".join(out) + "\n"

