"""The comparison: each arm against ``grouped``, paired by case and flipped by source bundle.

Per case, an arm's score is its mean task success over the repeats that could be
scored. The difference to ``grouped`` is paired on the same case, and the
switch_eval sign-flip test runs over declared source bundles. Legacy cases
retain their template groups. An arm passes when the
lower bound of the interval is above minus delta (design D9); an interval that
contains zero reads "no change". Cases an arm or ``grouped`` could not score are
listed, never counted as failures. A missing or unscored session makes the
whole round inconclusive; its available paired statistics remain visible.
Frozen ``discordance-exact`` rounds require independent binary case pairs and
use conservative exact binomial bounds instead of the historical sign flip.
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
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
# Frozen decision methods are the closed statistical protocols declared by the design.
DECISION_METHODS = ("grouped-sign-flip", "discordance-exact")
DEFAULT_DECISION_METHOD, EXACT_DECISION_METHOD = DECISION_METHODS


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
    return {result.case_id for result in results if result.wrong_write}


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
        "write_attempts_total": sum(r.write_attempts for r in results),
        "write_calls_served_total": sum(r.write_calls_served for r in results),
        "committed_effects_total": sum(r.committed_effects for r in results),
        "unexpected_mutations_total": sum(r.unexpected_mutations for r in results),
        "effect_unknown_calls_total": sum(r.effect_unknown_calls for r in results),
        "unscored_outcomes": dict(Counter(r.outcome for r in results if not r.scored)),
        "first_correct_found": len(found),
        "first_correct_ordinal_mean": _mean(found),
        "schema_bytes_mean": _mean([r.schema_bytes for r in results if r.schema_bytes is not None]),
        "input_tokens_mean": _mean([r.input_tokens for r in scored if r.input_tokens is not None]),
        "output_tokens_mean": _mean([r.output_tokens for r in scored if r.output_tokens is not None]),
    }


def _discordance_exact(
    baseline: Sequence[CaseResult], candidate: Sequence[CaseResult], cases: Mapping[str, Case],
    *, expected_repeats: int | None, delta: float,
) -> dict:
    """R2-S2 bounds complete independent binary pairs; never turn repeat means into Bernoulli trials."""
    unavailable = {"evaluable": False, "method": EXACT_DECISION_METHOD,
                   "mean_difference": None, "interval_95": [None, None]}
    if type(expected_repeats) is not int or expected_repeats != 1:
        return {**unavailable, "ineligible_reason": "discordance-exact requires a frozen single repeat"}
    clusters = [case.fixture_cluster for case in cases.values()]
    if (any(not isinstance(cluster, str) or not cluster.strip() for cluster in clusters)
            or len(set(clusters)) != len(cases)):
        return {**unavailable, "ineligible_reason": "discordance-exact requires one unique declared fixture cluster per case"}
    count = len(cases)
    if count < protocol.MIN_GROUPS:
        return {**unavailable, "ineligible_reason": f"{count} sampling units, fewer than {protocol.MIN_GROUPS}"}
    if any(len(rows) != count or {row.case_id for row in rows} != set(cases)
           or any(row.scored is not True or type(row.success) is not bool
                  or type(row.repeat) is not int or row.repeat != 1 for row in rows)
           for rows in (baseline, candidate)):
        return {**unavailable, "ineligible_reason": "discordance-exact requires exactly one scored binary pair per declared case"}
    from scipy.stats import binomtest

    baseline_by_case = {row.case_id: row.success for row in baseline}
    candidate_by_case = {row.case_id: row.success for row in candidate}
    losses = sum(baseline_by_case[case] and not candidate_by_case[case] for case in cases)
    gains = sum(candidate_by_case[case] and not baseline_by_case[case] for case in cases)
    # R2-S2 fixes each one-sided marginal confidence at 97.5% for the outer 95% interval.
    marginal_confidence = 0.975
    loss_upper, gain_upper = (
        float(binomtest(events, count, alternative="less").proportion_ci(
            confidence_level=marginal_confidence, method="exact",
        ).high)
        for events in (losses, gains)
    )
    return {"evaluable": True, "method": EXACT_DECISION_METHOD, "pairs": count,
            "losses": losses, "gains": gains, "ties": count - losses - gains,
            "mean_difference": (gains - losses) / count,
            "interval_95": [-loss_upper, gain_upper],
            "marginal_confidence": marginal_confidence,
            "non_inferiority": {"groups": count, "cases": count, "delta": delta,
                                "passes": -loss_upper > -delta}}


def verdict(comparison: Mapping, delta: float) -> tuple[str, str]:
    """``(state, reading)`` for one arm's paired comparison."""

    if not comparison["evaluable"]:
        return INCONCLUSIVE, comparison.get("ineligible_reason", "no paired cases")
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
    expected_repeats: int | None = None,
    expected_arms: Sequence[str] | None = None,
    decision_method: str = DEFAULT_DECISION_METHOD,
) -> dict:
    """Every arm against ``grouped`` on the cases both could score."""

    check_delta(delta)
    if decision_method not in DECISION_METHODS:
        raise ValueError(f"unknown decision method: {decision_method}")
    means = _case_means(results)
    by_arm = defaultdict(list)
    for result in results:
        by_arm[result.arm].append(result)
    for arm in expected_arms or ():
        by_arm.setdefault(arm, [])
    if BASELINE not in by_arm:
        raise ValueError(f"no sessions of the baseline arm {BASELINE}")
    groups = {case_id: case.cluster_id if decision_method == DEFAULT_DECISION_METHOD else case.fixture_cluster
              for case_id, case in cases.items()}
    baseline_wrong = _wrong_write_cases(by_arm[BASELINE])
    present = {(result.arm, result.case_id, result.repeat) for result in results}
    missing = {
        arm: [{"case_id": case, "repeat": repeat} for case in cases
              for repeat in range(1, expected_repeats + 1) if (arm, case, repeat) not in present]
        if expected_repeats is not None and (decision_method == DEFAULT_DECISION_METHOD
                                               or type(expected_repeats) is int) else None
        for arm in by_arm
    }
    unscored = {
        arm: [{"case_id": result.case_id, "repeat": result.repeat,
               "outcome": result.outcome, "reasons": result.reasons}
              for result in arm_results if not result.scored]
        for arm, arm_results in by_arm.items()
    }
    arms = {}
    for arm, arm_results in sorted(by_arm.items()):
        entry: dict = {"secondary": _secondary(arm_results)}
        wrong = _wrong_write_cases(arm_results)
        entry["wrong_write_cases"] = sorted(wrong)
        entry["unexpected_mutation_cases"] = sorted({r.case_id for r in arm_results if r.unexpected_mutations})
        entry["unknown_effect_cases"] = sorted({r.case_id for r in arm_results if r.effect_unknown_calls})
        entry["missing_sessions"] = missing[arm]
        entry["unscored_sessions"] = unscored[arm]
        if arm != BASELINE:
            paired = sorted(set(means.get(arm, {})) & set(means.get(BASELINE, {})))
            excluded = sorted(set(cases) - set(paired))
            differences = {case: means[arm][case] - means[BASELINE][case] for case in paired}
            comparison = (
                stats.compare(differences, groups, seed=seed, delta=delta)
                if decision_method == DEFAULT_DECISION_METHOD else
                _discordance_exact(by_arm[BASELINE], arm_results, cases, expected_repeats=expected_repeats, delta=delta)
            )
            state, reading = verdict(comparison, delta)
            complete = not (missing[arm] or missing[BASELINE] or unscored[arm]
                            or unscored[BASELINE] or excluded)
            if not complete:
                state, reading = INCONCLUSIVE, "the paired round has missing or unscored sessions"
            excess = len(wrong) - len(baseline_wrong)
            entry.update(
                paired_cases=len(paired),
                excluded_cases=excluded,
                comparison=comparison,
                paired_differences=differences,
                sampling_units={case: groups[case] for case in paired},
                delta=delta,
                verdict=state,
                reading=reading,
                wrong_write_excess=excess,
                wrong_write_ok=excess <= WRONG_WRITE_TOLERANCE,
                passes=state == PASS and excess <= WRONG_WRITE_TOLERANCE,
            )
        arms[arm] = entry
    report = {"baseline": BASELINE, "delta": delta, "seed": seed,
              "expected_repeats": expected_repeats, "arms": arms}
    if decision_method != DEFAULT_DECISION_METHOD:
        report["decision_method"] = decision_method
    return report


def format_readout(report: Mapping) -> str:
    lines = [f"baseline {report['baseline']}, delta {report['delta']}, seed {report['seed']}"]
    if "decision_method" in report:
        lines.append(f"decision method {report['decision_method']}")
    for arm, entry in report["arms"].items():
        secondary = entry["secondary"]
        lines.append(
            f"{arm}: {secondary['scored_sessions']}/{secondary['sessions']} sessions scored, "
            f"success {secondary['success_rate']}, calls {secondary['calls_mean']}, "
            f"refusals {secondary['argument_refusals_total']}, schema bytes {secondary['schema_bytes_mean']}, "
            f"input tokens {secondary['input_tokens_mean']}"
        )
        missing = entry["missing_sessions"]
        lines.append(f"  missing sessions: {len(missing) if missing is not None else 'unknown (no frozen repeat count)'}; "
                     f"unscored sessions: {len(entry['unscored_sessions'])}; "
                     f"unknown effects: {secondary['effect_unknown_calls_total']}; "
                     f"unexpected mutations: {secondary['unexpected_mutations_total']}")
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
