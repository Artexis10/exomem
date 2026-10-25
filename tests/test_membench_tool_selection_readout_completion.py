"""A partial paired round cannot produce a whole-round pass or hide known writes."""

from dataclasses import replace

import pytest
from membench.utility.tool_selection import readout
from membench.utility.tool_selection.cases import dev_cases
from membench.utility.tool_selection.score import CaseResult


def completed_round():
    cases = {case.case_id: case for case in dev_cases()}
    results = [
        CaseResult(case.case_id, case.template_id, case.kind, arm, repeat, "ok", True, True)
        for case in cases.values()
        for arm in ("grouped", "nested")
        for repeat in (1, 2)
    ]
    return cases, results


@pytest.mark.parametrize("arm", ["grouped", "nested"])
def test_one_unscored_repeat_prevents_a_pass_even_when_every_case_remains_paired(arm):
    cases, results = completed_round()
    unavailable = next(iter(cases))
    results = [
        replace(result, scored=False, success=False, reasons=["canonical read unavailable"])
        if (result.arm, result.case_id, result.repeat) == (arm, unavailable, 2) else result
        for result in results
    ]

    report = readout.readout(results, cases, expected_repeats=2, expected_arms=("grouped", "nested"))
    candidate = report["arms"]["nested"]

    assert candidate["paired_cases"] == len(cases)
    assert candidate["comparison"]["mean_difference"] == 0.0
    assert candidate["missing_sessions"] == []
    assert report["arms"][arm]["unscored_sessions"] == [
        {"case_id": unavailable, "repeat": 2, "outcome": "ok", "reasons": ["canonical read unavailable"]}
    ]
    assert candidate["verdict"] == readout.INCONCLUSIVE
    assert not candidate["passes"]


def test_excluding_an_unscored_case_keeps_subset_statistics_but_cannot_pass_the_round():
    cases, results = completed_round()
    unavailable = next(iter(cases))
    results = [
        replace(result, scored=False, success=False)
        if (result.arm, result.case_id) == ("nested", unavailable) else result
        for result in results
    ]

    candidate = readout.readout(results, cases, expected_repeats=2)["arms"]["nested"]

    assert candidate["excluded_cases"] == [unavailable]
    assert candidate["comparison"]["mean_difference"] == 0.0
    assert candidate["verdict"] == readout.INCONCLUSIVE
    assert not candidate["passes"]


def test_a_known_wrong_write_stays_visible_when_its_session_cannot_be_fully_scored():
    cases, results = completed_round()
    affected = next(case.case_id for case in cases.values() if case.kind == "read")
    results = [
        replace(result, scored=False, success=False, wrong_write=True, write_calls_served=1)
        if (result.arm, result.case_id, result.repeat) == ("nested", affected, 1) else result
        for result in results
    ]

    candidate = readout.readout(results, cases, expected_repeats=2)["arms"]["nested"]

    assert candidate["wrong_write_cases"] == [affected]
    assert candidate["wrong_write_excess"] == 1
    assert not candidate["passes"]
