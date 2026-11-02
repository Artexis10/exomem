"""Pass rates for the store-identity cases (07 to 09): the answer's R1, R5 and R7 per case, model and arm,
the write rate (R6) and the candidate-minus-baseline difference with a cluster bootstrap over the repeats.

R1 and R5 are the judge's criteria. R7 holds only when the agent made a committed save (the `write_committed` check)
and the judge found no refusal and no request to choose a store. R6 is read from the call log: it reports whether the
agent wrote, and passes or fails nothing. An unknown run (a run error, a missing judge answer) is left out of every
rate and listed at the end.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path

from . import checks
from .report import BOOTSTRAP_RESAMPLES, BOOTSTRAP_SEED, _pct, _verdict_of, passed, rate, read_rows

WRITE_CHECK = "write_committed"
#: The mention that the audit table shows, and nothing more. It never scores a run: the judge decides R5 from the
#: whole answer, and this only lets a reader see the words behind each verdict.
AUDIT_WORD = "personal"
CONTEXT_CHARS = 70


def wrote(row: dict) -> bool | None:
    """Whether the agent made a committed save, from the call log; None for a run that ended in an error."""
    if row["error"] is not None:
        return None
    return bool(checks.committed_writes(tuple(row["calls"]), "create_note"))


def r7(row: dict) -> bool | None:
    judged = passed(row, "R7")
    written = {checks.PASS: True, checks.FAIL: False}.get(_verdict_of(row["checks"], WRITE_CHECK))
    if judged is False or written is False:
        return False
    return True if judged and written else None


#: The measure of each case, by criterion.
MEASURES: dict[str, Callable[[dict], bool | None]] = {
    "R1": lambda row: passed(row, "R1"),
    "R5": lambda row: passed(row, "R5"),
    "R6 write": wrote,
    "R7": r7,
}


def paired_interval(
    pairs: Sequence[tuple[bool, bool]],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
    """95% percentile interval of the mean (candidate - baseline), resampling the (model, repeat) clusters."""
    rng = random.Random(seed)
    diffs = [int(candidate) - int(baseline) for baseline, candidate in pairs]
    means = sorted(sum(rng.choices(diffs, k=len(diffs))) / len(diffs) for _ in range(resamples))
    return means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1]


def bootstrap_profiles(row: dict) -> str:
    """How the agent called bootstrap: `session`, `compact`, or `none`, one word per call, in order."""
    kinds = [
        str((c["arguments"].get("operation") or {}).get("profile", "compact"))
        for c in row["calls"]
        if c["tool"] == "bootstrap"
    ]
    return "+".join(kinds) or "none"


def mentions(text: str) -> list[str]:
    found, lowered, start = [], text.lower(), 0
    while (at := lowered.find(AUDIT_WORD, start)) != -1:
        found.append(
            text[max(0, at - CONTEXT_CHARS) : at + len(AUDIT_WORD) + CONTEXT_CHARS].replace(
                "\n", " "
            )
        )
        start = at + len(AUDIT_WORD)
    return found


def build(paths: Sequence[Path], *, baseline: str, candidate: str) -> str:
    rows, replaced = read_rows(paths)
    arms = (baseline, candidate)
    rows = [r for r in rows if r.get("arm") in arms]
    models = sorted({r["model"] for r in rows})
    cell: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        cell[(row["scenario"], row["model"], row["arm"])].append(row)
    cases = sorted({r["scenario"] for r in rows})
    shas = {r["arm"]: r.get("product_sha") for r in rows}

    def measures_of(case: str) -> list[str]:
        """The criteria the case's scenario judges; the wrong-store case (R5) also reports whether the agent wrote."""
        judged = {c["check"] for r in rows if r["scenario"] == case for c in r["criteria"]}
        return [
            n
            for n in ("R1", "R5", "R6 write", "R7")
            if n in judged or (n == "R6 write" and "R5" in judged)
        ]

    out = [
        f"Baseline `{baseline}` at `{shas.get(baseline)}`. Candidate `{candidate}` at `{shas.get(candidate)}`.",
        f"Agents: {', '.join(models)}. Judge: {', '.join(sorted({r['judge_model'] for r in rows}))}. "
        f"Runs: {len(rows)}, of which {sum(1 for r in rows if r['error'] is not None)} ended in an error. "
        f"apiKeySource over all runs: {sorted({str(r.get('api_key_source')) for r in rows})}.",
        "",
        "## Pass counts by case, model and arm",
        "",
        "Cells are passes/known runs (R6: runs that wrote/known runs). The difference is candidate minus baseline.",
        "",
        "| case | measure | model | baseline | candidate | difference |",
        "|---|---|---|---|---|---|",
    ]
    for case in cases:
        for name in measures_of(case):
            for model in [*models, "both"]:
                chosen = models if model == "both" else [model]
                base, cand = (
                    rate([MEASURES[name](r) for m in chosen for r in cell[(case, m, arm)]])
                    for arm in arms
                )
                diff = (
                    f"{cand[0] / cand[1] - base[0] / base[1]:+.2f}"
                    if base[1] and cand[1]
                    else "n/a"
                )
                out.append(
                    f"| {case} | {name} | {model} | {_pct(*base)} | {_pct(*cand)} | {diff} |"
                )

    out += [
        "",
        "## Difference by case, both models together",
        "",
        "Cluster bootstrap: one cluster is a (model, repeat) pair, which ran both arms on the same case; the interval",
        "resamples those paired differences. Each case has few clusters, so the interval is coarse. With three cases",
        "an interval over cases is not meaningful, so none is given.",
        "",
        "| case | measure | clusters | baseline | candidate | difference | 95% interval |",
        "|---|---|---|---|---|---|---|",
    ]
    for case in cases:
        for name in measures_of(case):
            pairs = []
            for model in models:
                by_repeat = {
                    arm: {r["repeat"]: MEASURES[name](r) for r in cell[(case, model, arm)]}
                    for arm in arms
                }
                for repeat in sorted(set(by_repeat[baseline]) & set(by_repeat[candidate])):
                    base, cand = by_repeat[baseline][repeat], by_repeat[candidate][repeat]
                    if base is not None and cand is not None:
                        pairs.append((base, cand))
            base_all = rate([MEASURES[name](r) for m in models for r in cell[(case, m, baseline)]])
            cand_all = rate([MEASURES[name](r) for m in models for r in cell[(case, m, candidate)]])
            if pairs:
                low, high = paired_interval(pairs)
                mean = sum(int(c) - int(b) for b, c in pairs) / len(pairs)
                shown = f"{mean:+.2f} | [{low:+.2f}, {high:+.2f}]"
            else:
                shown = "n/a | n/a"
            out.append(
                f"| {case} | {name} | {len(pairs)} | {_pct(*base_all)} | {_pct(*cand_all)} | {shown} |"
            )

    out += [
        "",
        "## Bootstrap calls",
        "",
        "How each agent called bootstrap (the profile of each call, in order). The scripted server serves the session",
        "profile for any `profile: session` call and does not check the digest.",
        "",
        "| case | arm | model | calls per run |",
        "|---|---|---|---|",
    ]
    for case in cases:
        for arm in arms:
            for model in models:
                runs = cell[(case, model, arm)]
                counts: dict[str, int] = defaultdict(int)
                for r in runs:
                    counts[bootstrap_profiles(r)] += 1
                shown = ", ".join(f"{n}x {kind}" for kind, n in sorted(counts.items()))
                out.append(f"| {case} | {arm} | {model} | {shown or 'no runs'} |")

    out += [
        "",
        f'## Audit of the wrong-store case: the answer\'s mentions of "{AUDIT_WORD}"',
        "",
        "Context around each mention, for a reader to check the judge. R1 and R5 are the judge's verdicts; the",
        "write column is from the call log.",
        "",
        "| arm | model | repeat | wrote | R1 | R5 | mentions | judge's R5 reason |",
        "|---|---|---|---|---|---|---|---|",
    ]

    def show(value: bool | None) -> str:
        return {True: "pass", False: "fail", None: "unknown"}[value]

    wrong_store = [r for r in rows if any(c["check"] == "R5" for c in r["criteria"])]
    for r in sorted(wrong_store, key=lambda r: (r["arm"], r["model"], r["repeat"])):
        reason = next((c["detail"] for c in r["criteria"] if c["check"] == "R5"), "")
        found = " / ".join(f"...{m}..." for m in mentions(r["final_text"] or "")) or "none"
        out.append(
            f"| {r['arm']} | {r['model']} | {r['repeat']} | {wrote(r)} | {show(passed(r, 'R1'))} | "
            f"{show(passed(r, 'R5'))} | {found.replace('|', '/')} | {reason.replace('|', '/')[:200]} |"
        )

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
    if replaced:
        out += ["", "Rows replaced by a later run of the same arm, model, case and repeat:", ""]
        out += ["| arm | model | case | repeat | reason |", "|---|---|---|---|---|"]
        for r in replaced:
            reason = (r["error"] or "ended without an error").replace("|", "/")[:160]
            out.append(
                f"| {r['arm']} | {r['model']} | {r['scenario']} | {r['repeat']} | {reason} |"
            )
    return "\n".join(out) + "\n"
