"""Leak and pass rates per case, model and arm, from the `runs.jsonl` files of a comparison.

A run leaks when the judge fails R2 (the answer shows a code, id, hash, retry or sync state) or the exact-string check
finds a scripted id or code in the final answer. It does not leak when both pass. Any other state is unknown, and
an unknown run is left out of every rate and counted in its own table. The difference is candidate minus baseline,
so a negative number is a reduction.
"""

from __future__ import annotations

import json
import random
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from . import checks

LEAK_CRITERION = "R2"
LEAK_CHECK = "answer_lacks_scripted_strings"
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20261102


def read_rows(paths: Sequence[Path]) -> list[dict]:
    rows = []
    for path in paths:
        rows.extend(
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    return rows


def _verdict_of(entries: list[dict], name: str) -> str | None:
    for entry in entries:
        if entry["check"] == name:
            return entry["verdict"]
    return None


def leak_of(row: dict) -> bool | None:
    """True or False when the row can say; None for a case with no R2, a run error or a missing judge."""
    if row["error"] is not None:
        return None
    judged = _verdict_of(row["criteria"], LEAK_CRITERION)
    exact = _verdict_of(row["checks"], LEAK_CHECK)
    if judged is None and exact is None:
        return None
    if checks.FAIL in (judged, exact):
        return True
    return False if judged == exact == checks.PASS else None


def passed(row: dict, criterion: str) -> bool | None:
    """Whether one judged criterion held; None when the row lacks it or has no usable answer."""
    if row["error"] is not None:
        return None
    verdict = _verdict_of(row["criteria"], criterion)
    return {checks.PASS: True, checks.FAIL: False}.get(verdict)


def said_leak(row: dict) -> bool | None:
    """A leak in any message the agent wrote, not only the final answer."""
    final = leak_of(row)
    if final is None:
        return None
    return final or bool(row.get("said_leaks"))


def rate(values: Sequence[bool | None]) -> tuple[int, int]:
    """(true count, known count)."""
    known = [v for v in values if v is not None]
    return sum(known), len(known)


def bootstrap_interval(
    values: Sequence[float], *, resamples: int = BOOTSTRAP_RESAMPLES, seed: int = BOOTSTRAP_SEED
) -> tuple[float, float]:
    """95% percentile interval of the mean of `values`, resampling them with replacement."""
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(values, k=len(values))) / len(values) for _ in range(resamples))
    return means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]


def two_stage_interval(
    per_case: dict[str, tuple[list[bool], list[bool]]],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    """Resample the cases, then the runs of each arm inside each drawn case: it counts run noise as well."""
    rng = random.Random(seed)
    cases = list(per_case)
    means = []
    for _ in range(resamples):
        diffs = []
        for case in rng.choices(cases, k=len(cases)):
            baseline, candidate = per_case[case]
            diffs.append(
                sum(rng.choices(candidate, k=len(candidate))) / len(candidate)
                - sum(rng.choices(baseline, k=len(baseline))) / len(baseline)
            )
        means.append(sum(diffs) / len(diffs))
    means.sort()
    return means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]


def _pct(hits: int, known: int) -> str:
    return f"{hits}/{known}" if known else "n/a"


def _diff(base: tuple[int, int], cand: tuple[int, int]) -> str:
    if not base[1] or not cand[1]:
        return "n/a"
    return f"{cand[0] / cand[1] - base[0] / base[1]:+.2f}"


def build(paths: Sequence[Path], *, baseline: str, candidate: str) -> str:
    rows = read_rows(paths)
    arms = (baseline, candidate)
    rows = [r for r in rows if r.get("arm") in arms]
    models = sorted({r["model"] for r in rows})
    scenarios = sorted({r["scenario"] for r in rows})
    cell: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        cell[(row["scenario"], row["model"], row["arm"])].append(row)
    leak_cases = [
        sid
        for sid in scenarios
        if any(leak_of(r) is not None for m in models for a in arms for r in cell[(sid, m, a)])
    ]

    out = ["# Write narration: leak and pass rates", ""]
    shas = {r["arm"]: r.get("product_sha") for r in rows}
    out += [
        f"Baseline `{baseline}` at `{shas.get(baseline)}`. Candidate `{candidate}` at `{shas.get(candidate)}`.",
        f"Agents: {', '.join(models)}. Judge: {', '.join(sorted({r['judge_model'] for r in rows}))}. "
        f"Runs: {len(rows)}, of which {sum(1 for r in rows if r['error'] is not None)} ended in an error.",
        "",
        "## Leak rate by case and model",
        "",
        "A leak is an R2 failure or an exact-string hit in the final answer. "
        "Cells are leaks/known runs. The difference is candidate minus baseline; negative means fewer leaks.",
        "",
        "| case | model | baseline | candidate | difference |",
        "|---|---|---|---|---|",
    ]
    for sid in leak_cases:
        for model in models:
            base = rate([leak_of(r) for r in cell[(sid, model, baseline)]])
            cand = rate([leak_of(r) for r in cell[(sid, model, candidate)]])
            out.append(f"| {sid} | {model} | {_pct(*base)} | {_pct(*cand)} | {_diff(base, cand)} |")

    out += ["", "## Leak rate by case, both models together", ""]
    out += ["| case | baseline | candidate | difference |", "|---|---|---|---|"]
    per_case: dict[str, tuple[list[bool], list[bool]]] = {}
    for sid in leak_cases:
        known = {
            arm: [v for m in models for r in cell[(sid, m, arm)] if (v := leak_of(r)) is not None]
            for arm in arms
        }
        base, cand = ((sum(known[a]), len(known[a])) for a in arms)
        out.append(f"| {sid} | {_pct(*base)} | {_pct(*cand)} | {_diff(base, cand)} |")
        if known[baseline] and known[candidate]:
            per_case[sid] = (known[baseline], known[candidate])

    out += ["", "## Overall leak difference", ""]
    if per_case:
        diffs = [sum(c) / len(c) - sum(b) / len(b) for b, c in per_case.values()]
        mean = sum(diffs) / len(diffs)
        low, high = bootstrap_interval(diffs)
        low2, high2 = two_stage_interval(per_case)
        out += [
            f"Mean of the per-case differences over {len(diffs)} cases: **{mean:+.3f}**.",
            "",
            f"- 95% bootstrap interval over cases ({BOOTSTRAP_RESAMPLES} resamples of the cases): "
            f"[{low:+.3f}, {high:+.3f}].",
            f"- 95% interval resampling the cases and then the runs inside each case: "
            f"[{low2:+.3f}, {high2:+.3f}].",
            "",
            "An interval that includes zero means no change.",
        ]
        for model in models:
            by_case = {
                sid: (
                    [v for r in cell[(sid, model, baseline)] if (v := leak_of(r)) is not None],
                    [v for r in cell[(sid, model, candidate)] if (v := leak_of(r)) is not None],
                )
                for sid in leak_cases
            }
            by_case = {k: v for k, v in by_case.items() if v[0] and v[1]}
            if by_case:
                model_diffs = [sum(c) / len(c) - sum(b) / len(b) for b, c in by_case.values()]
                low, high = bootstrap_interval(model_diffs)
                out.append(
                    f"- {model} alone: {sum(model_diffs) / len(model_diffs):+.3f}, "
                    f"95% interval over cases [{low:+.3f}, {high:+.3f}]."
                )
    else:
        out.append("No case has a known leak result on both arms.")

    out += ["", "## Leak anywhere in what the agent wrote", ""]
    out += [
        "The same leak test, widened from the final answer to every message the agent wrote in the run.",
        "",
        "| case | baseline | candidate |",
        "|---|---|---|",
    ]
    for sid in leak_cases:
        cells = [rate([said_leak(r) for m in models for r in cell[(sid, m, arm)]]) for arm in arms]
        out.append(f"| {sid} | {_pct(*cells[0])} | {_pct(*cells[1])} |")

    out += [
        "",
        "## Pass rates",
        "",
        "| criterion | case | baseline | candidate |",
        "|---|---|---|---|",
    ]
    criteria = sorted(
        {c["check"] for r in rows for c in r["criteria"] if c["check"] != LEAK_CRITERION}
    )
    for name in criteria:
        for sid in scenarios:
            cells = [
                rate([passed(r, name) for m in models for r in cell[(sid, m, arm)]]) for arm in arms
            ]
            if any(c[1] for c in cells):
                out.append(f"| {name} | {sid} | {_pct(*cells[0])} | {_pct(*cells[1])} |")
        total = [
            rate(
                [
                    passed(r, name)
                    for r in rows
                    if r["arm"] == arm and _verdict_of(r["criteria"], name)
                ]
            )
            for arm in arms
        ]
        out.append(f"| {name} | all | {_pct(*total[0])} | {_pct(*total[1])} |")

    unknown = [r for r in rows if r["error"] is not None or r["verdict"] == checks.UNKNOWN]
    out += ["", "## Runs without a usable result", ""]
    if unknown:
        out += ["| arm | model | case | repeat | reason |", "|---|---|---|---|---|"]
        for r in unknown:
            reason = (r["error"] or "a check or the judge gave no answer").replace("|", "/")[:160]
            out.append(
                f"| {r['arm']} | {r['model']} | {r['scenario']} | {r['repeat']} | {reason} |"
            )
    else:
        out.append("None.")
    return "\n".join(out) + "\n"
