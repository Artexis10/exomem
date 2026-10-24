"""The comparison: each arm against ``grouped``, paired by case, flipped by template.

Per case, an arm's score is its mean task success over the repeats that could be
scored. The difference to ``grouped`` is paired on the same case, and the
switch_eval sign-flip test runs over template groups (not cases), so one
template's cases never count as independent evidence. An arm passes when the
lower bound of the interval is above minus delta (design D9); an interval that
contains zero reads "no change". Cases an arm or ``grouped`` could not score are
listed, never counted as failures.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

from membench.utility.switch_eval import protocol, stats
from membench.utility.switch_eval.session import load_session

from .cases import Case
from .resolve import Resolver
from .score import CaseResult, score_session

BASELINE = "grouped"
#: Design D9: arm cases with a write during a read task, minus the baseline's, at most 1.
WRONG_WRITE_TOLERANCE = 1

PASS = "pass"
FAIL = "fail"
INCONCLUSIVE = "inconclusive"


def check_delta(delta: float) -> float:
    if not protocol.DELTA_FLOOR <= delta <= protocol.DELTA_CAP:
        raise ValueError(
            f"delta {delta} is outside the protocol's range [{protocol.DELTA_FLOOR}, {protocol.DELTA_CAP}]"
        )
    return delta


def load_results(
    session_dirs: Sequence[Path], cases: Mapping[str, Case], resolver: Resolver
) -> list[CaseResult]:
    results = []
    for directory in session_dirs:
        session = load_session(directory)
        if session is None:
            continue
        calls = (session.get("facts") or {}).get("calls") or []
        results.append(score_session(cases[session["case_id"]], session, calls, resolver))
    return results


def _mean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _case_means(results: Sequence[CaseResult]) -> dict[str, dict[str, float]]:
    """``{arm: {case: mean success over the scored repeats}}``; an unscored case is absent."""

    scored: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for result in results:
        if result.scored:
            scored[result.arm][result.case_id].append(1.0 if result.success else 0.0)
    return {
        arm: {case: statistics.fmean(values) for case, values in per_case.items()}
        for arm, per_case in scored.items()
    }


def _wrong_write_cases(results: Sequence[CaseResult]) -> set[str]:
    return {result.case_id for result in results if result.scored and result.wrong_write}


def _secondary(results: Sequence[CaseResult]) -> dict:
    scored = [r for r in results if r.scored]
    found = [r.first_correct_ordinal for r in scored if r.first_correct_ordinal is not None]
    return {
        "sessions": len(results),
        "scored_sessions": len(scored),
        "success_rate": _mean([1.0 if r.success else 0.0 for r in scored]),
        "calls_mean": _mean([r.calls for r in scored]),
        "argument_refusals_total": sum(r.argument_refusals for r in scored),
        "argument_refusals_mean": _mean([r.argument_refusals for r in scored]),
        "unresolved_calls_total": sum(r.unresolved_calls for r in scored),
        "first_correct_found": len(found),
        "first_correct_ordinal_mean": _mean(found),
        "schema_bytes_mean": _mean([r.schema_bytes for r in results if r.schema_bytes is not None]),
        "input_tokens_mean": _mean([r.input_tokens for r in scored if r.input_tokens is not None]),
        "output_tokens_mean": _mean([r.output_tokens for r in scored if r.output_tokens is not None]),
    }


def verdict(comparison: Mapping, delta: float) -> tuple[str, str]:
    """``(state, reading)`` for one arm's paired comparison."""

    if not comparison["evaluable"]:
        return INCONCLUSIVE, "no paired cases"
    groups = comparison["non_inferiority"]["groups"]
    if groups < protocol.MIN_GROUPS:
        return INCONCLUSIVE, f"{groups} template groups, fewer than {protocol.MIN_GROUPS}"
    lower, upper = comparison["interval_95"]
    if lower is None:
        return INCONCLUSIVE, "the test cannot bound the interval from below"
    if lower <= -delta:
        return FAIL, f"lower bound {lower:+.3f} is not above -{delta}"
    if lower <= 0 and (upper is None or upper >= 0):
        return PASS, "no change (the interval contains zero)"
    if lower > 0:
        return PASS, "better"
    return PASS, "worse, within the margin"


def readout(
    results: Sequence[CaseResult],
    cases: Mapping[str, Case],
    *,
    delta: float = protocol.DELTA_FLOOR,
    seed: int = 0,
) -> dict:
    """Every arm against ``grouped`` on the cases both could score."""

    check_delta(delta)
    means = _case_means(results)
    by_arm = defaultdict(list)
    for result in results:
        by_arm[result.arm].append(result)
    if BASELINE not in by_arm:
        raise ValueError(f"no sessions of the baseline arm {BASELINE}")
    groups = {case_id: case.template_id for case_id, case in cases.items()}
    baseline_wrong = _wrong_write_cases(by_arm[BASELINE])
    arms = {}
    for arm, arm_results in sorted(by_arm.items()):
        entry: dict = {"secondary": _secondary(arm_results)}
        wrong = _wrong_write_cases(arm_results)
        entry["wrong_write_cases"] = sorted(wrong)
        if arm != BASELINE:
            paired = sorted(set(means.get(arm, {})) & set(means.get(BASELINE, {})))
            differences = {case: means[arm][case] - means[BASELINE][case] for case in paired}
            comparison = stats.compare(differences, groups, seed=seed, delta=delta)
            state, reading = verdict(comparison, delta)
            excess = len(wrong) - len(baseline_wrong)
            entry.update(
                paired_cases=len(paired),
                excluded_cases=sorted(set(cases) - set(paired)),
                comparison=comparison,
                delta=delta,
                verdict=state,
                reading=reading,
                wrong_write_excess=excess,
                wrong_write_ok=excess <= WRONG_WRITE_TOLERANCE,
                passes=state == PASS and excess <= WRONG_WRITE_TOLERANCE,
            )
        arms[arm] = entry
    return {"baseline": BASELINE, "delta": delta, "seed": seed, "arms": arms}


def format_readout(report: Mapping) -> str:
    lines = [f"baseline {report['baseline']}, delta {report['delta']}, seed {report['seed']}"]
    for arm, entry in report["arms"].items():
        secondary = entry["secondary"]
        lines.append(
            f"{arm}: {secondary['scored_sessions']}/{secondary['sessions']} sessions scored, "
            f"success {secondary['success_rate']}, calls {secondary['calls_mean']}, "
            f"refusals {secondary['argument_refusals_total']}, schema bytes {secondary['schema_bytes_mean']}, "
            f"input tokens {secondary['input_tokens_mean']}"
        )
        if arm == report["baseline"]:
            continue
        comparison = entry["comparison"]
        lines.append(
            f"  vs {report['baseline']}: mean difference {comparison.get('mean_difference')} "
            f"over {entry['paired_cases']} cases, interval {comparison.get('interval_95')}; "
            f"{entry['verdict']} ({entry['reading']}); wrong-write excess {entry['wrong_write_excess']}; "
            f"passes {entry['passes']}"
        )
    return "\n".join(lines)


def write_readout(report: Mapping, path: Path) -> None:
    path.write_text(json.dumps(report, indent=2, sort_keys=True, default=float), encoding="utf-8")
