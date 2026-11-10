"""The replay report: for each set of cases, the paired difference between two arms on stage-B answers.

An answer passes when every named judged criterion holds in the blind batch (`judge_batch.py`) and every deterministic
check of the case passes. A run error, a missing judgment, an unreadable one and a missing arm are `unknown`: they are
counted and listed, and never read as a failure or as zero. For each case the known (model, repeat) pairs are averaged;
the interval is a 95% percentile bootstrap over cases (`store_report.paired_interval`). Reply length is the characters of
the final answer, compared as a per-case log ratio.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path

from . import checks
from .judge_batch import agreement_line
from .replay_cases import RUBRIC
from .report import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED, read_rows
from .store_report import paired_interval

DEFAULT_CRITERIA = tuple(criterion["id"] for criterion in RUBRIC)
Value = Callable[[dict], float | None]


def run_key(row: dict) -> tuple:
    return row["scenario"], row["arm"], row["model"], row["repeat"]


def verdict_of(row: dict, judged: dict | None, criteria: Sequence[str]) -> str:
    """Pass, fail or unknown for one run, from its deterministic checks and the named criteria of its judgment."""
    if row["error"] is not None:
        return checks.UNKNOWN
    found = {o["check"]: o for o in (judged or {}).get("criteria", [])}
    judged_outcomes = [
        checks.Outcome(**found[name]) if name in found else checks.Outcome(name, checks.UNKNOWN, "no judgment")
        for name in criteria
    ]
    return checks.verdict(None, [checks.Outcome(**o) for o in row["checks"]], judged_outcomes)


def known_pairs(rows: Sequence[dict], baseline: str, candidate: str, value: Value) -> tuple[dict[str, list[tuple[float, float]]], int]:
    """Per case, the (baseline, candidate) values of each (model, repeat) where both arms have one, and how many
    (case, model, repeat) clusters lack one on either arm."""
    clusters: dict[tuple, dict[str, float | None]] = defaultdict(dict)
    for row in rows:
        clusters[(row["scenario"], row["model"], row["repeat"])][row["arm"]] = value(row)
    per_case: dict[str, list[tuple[float, float]]] = defaultdict(list)
    unknown = 0
    for (case, _model, _repeat), values in clusters.items():
        pair = (values.get(baseline), values.get(candidate))
        if None in pair:
            unknown += 1
        else:
            per_case[case].append(pair)
    return per_case, unknown


def difference(per_case: dict[str, list[tuple[float, float]]], cases: Sequence[str], unknown_pairs: int, *, log: bool = False) -> str:
    """One sentence: the mean per-case difference with its interval, the cases it rests on and the unknown ones."""
    means = {
        case: (sum(b for b, _ in pairs) / len(pairs), sum(c for _, c in pairs) / len(pairs))
        for case, pairs in per_case.items()
        if case in cases and pairs
    }
    missing = [case for case in cases if case not in means]
    rest = f"unknown cases ({len(missing)}): {', '.join(missing) or 'none'}; unknown (case, model, repeat) pairs: {unknown_pairs}"
    if not means:
        return f"no paired case of {len(cases)}; {rest}."
    low, high = paired_interval(list(means.values()))
    mean = sum(c - b for b, c in means.values()) / len(means)
    verdict = "no change" if low <= 0 <= high else "interval excludes zero"
    shown = (
        f"mean log ratio {mean:+.3f} (candidate over baseline x{math.exp(mean):.2f}; 95% interval {low:+.3f} to {high:+.3f}; {verdict})"
        if log
        else f"difference {mean:+.2f} (95% interval {low:+.2f} to {high:+.2f}; {verdict})"
    )
    return f"{shown} over {len(means)} paired case(s) of {len(cases)}; {rest}."


def check_verdict(row: dict) -> str | None:
    """The deterministic checks alone: fail if any fails, pass if all pass, None when there is none or a gap."""
    states = {o["verdict"] for o in row["checks"] if o["verdict"] != checks.NOT_APPLICABLE}
    return checks.FAIL if checks.FAIL in states else checks.PASS if states == {checks.PASS} else None


def judge_verdict(judged: dict | None, criteria: Sequence[str]) -> str | None:
    states = [o["verdict"] for o in (judged or {}).get("criteria", []) if o["check"] in criteria]
    return checks.FAIL if checks.FAIL in states else checks.PASS if states and set(states) == {checks.PASS} else None


def hook_counts(rows: Sequence[dict]) -> str:
    seen = [r.get("hook_invocations") for r in rows]
    return f"{sum((n or 0) > 0 for n in seen)} with, {sum(n == 0 for n in seen)} without, {sum(n is None for n in seen)} unknown, of {len(rows)} runs"


def build(
    runs: Sequence[Path], *, judged: Path | None, case_sets: Path, baseline: str, candidate: str, criteria: Sequence[str] = DEFAULT_CRITERIA
) -> str:
    sets_of = json.loads(case_sets.read_text(encoding="utf-8"))
    rows = [r for r in read_rows(runs)[0] if r.get("arm") in (baseline, candidate)]
    outside = sorted({r["scenario"] for r in rows} - set(sets_of))
    if outside:
        raise SystemExit(f"cases {outside} are in the runs but not in the case-set map {case_sets}")
    agreement, judged_rows = (None, {})
    if judged is not None:
        data = json.loads(judged.read_text(encoding="utf-8"))
        agreement = data["agreement"]
        judged_rows = {run_key(r): r for r in data["rows"] if r["stage"] == "B"}
    verdicts = {run_key(r): verdict_of(r, judged_rows.get(run_key(r)), criteria) for r in rows}

    def passes(row: dict) -> float | None:
        return {checks.PASS: 1.0, checks.FAIL: 0.0}.get(verdicts[run_key(row)])

    def length(row: dict) -> float | None:
        return math.log(len(row["final_text"])) if row["error"] is None and row.get("final_text") else None

    shas = {r["arm"]: r.get("product_sha") for r in rows}
    models = sorted({r["model"] for r in rows})
    sets = sorted(set(sets_of.values()))
    in_set = {name: sorted(case for case, s in sets_of.items() if s == name) for name in sets}
    out = [
        f"Baseline `{baseline}` at `{shas.get(baseline)}`. Candidate `{candidate}` at `{shas.get(candidate)}`.",
        f"Stage-B runs read: {len(rows)}, of which {sum(r['error'] is not None for r in rows)} ended in an error (unknown). "
        f"Counting criteria: {', '.join(criteria)} and every deterministic check of the case. Models: {', '.join(models)}.",
        (agreement_line(agreement) if agreement else "No judge batch was given: every judged criterion is unknown.")
        + ("" if agreement and not agreement["incomplete"] else " The result below is incomplete."),
        "Runs whose stream shows a hook response (information only, not a gate): "
        + "; ".join(f"{arm} {hook_counts([r for r in rows if r['arm'] == arm])}" for arm in (baseline, candidate))
        + ".",
        "",
        f"Differences are candidate minus baseline. Per case, the known (model, repeat) pairs are averaged; the interval "
        f"is a 95% percentile bootstrap over cases ({BOOTSTRAP_RESAMPLES} resamples, seed {BOOTSTRAP_SEED}). "
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
        pairs = known_pairs(mine, baseline, candidate, passes)[0].get(case)
        shown = f"{sum(c - b for b, c in pairs) / len(pairs):+.2f}" if pairs else "unknown"
        out.append(f"| {case} | {sets_of[case]} | {counts[0]} | {counts[1]} | {shown} |")
    split = [
        r
        for r in rows
        if r["error"] is None
        and {s for s in (check_verdict(r), judge_verdict(judged_rows.get(run_key(r)), criteria)) if s} == {checks.PASS, checks.FAIL}
    ]
    out += ["", "## Deterministic checks and the judge disagree", ""]
    out += [f"- {r['arm']} {r['model']} {r['scenario']}#{r['repeat']}" for r in split] or ["None."]
    unusable = [r for r in rows if verdicts[run_key(r)] == checks.UNKNOWN]
    out += ["", "## Runs without a usable result", ""]
    out += [
        f"- {r['arm']} {r['model']} {r['scenario']}#{r['repeat']}: {(r['error'] or 'a judgment or a check is missing')[:160]}"
        for r in unusable
    ] or ["None."]
    return "\n".join(out) + "\n"

