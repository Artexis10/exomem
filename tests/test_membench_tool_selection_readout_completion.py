"""A partial paired round cannot produce a whole-round pass or hide known writes."""

import json
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


def independent_round(count, *, losses=0, gains=0):
    prototype = next(case for case in dev_cases() if case.kind == "read")
    cases = {f"case-{index}": replace(prototype, case_id=f"case-{index}", fixture_cluster=f"fixture-{index}")
             for index in range(count)}
    results = [
        CaseResult(case_id, case.template_id, case.kind, arm, 1, "ok", True,
                   not (arm == "nested" and index < losses or arm == "grouped" and losses <= index < losses + gains))
        for index, (case_id, case) in enumerate(cases.items()) for arm in ("grouped", "nested")
    ]
    return cases, results


@pytest.mark.parametrize("count,losses,gains,expected", [
    (9, 0, 0, readout.FAIL), (70, 0, 0, readout.PASS),
    (70, 2, 0, readout.PASS), (70, 3, 0, readout.FAIL), (70, 0, 2, readout.PASS),
])
def test_exact_discordance_counts_unseen_losses_and_keeps_the_frozen_margin(count, losses, gains, expected):
    cases, results = independent_round(count, losses=losses, gains=gains)

    report = readout.readout(results, cases, expected_repeats=1, decision_method="discordance-exact")
    candidate = report["arms"]["nested"]
    comparison = candidate["comparison"]

    assert candidate["verdict"] == expected
    assert comparison["losses"] == losses and comparison["gains"] == gains
    assert comparison["mean_difference"] == pytest.approx((gains - losses) / count)
    assert comparison["interval_95"][0] < 0 < comparison["interval_95"][1]
    assert candidate["passes"] is (expected == readout.PASS)
    if expected == readout.PASS:
        assert candidate["reading"] == "no change (the interval contains zero)"
    if losses == 2:
        assert comparison["interval_95"][0] == pytest.approx(-0.09942893009176197)


@pytest.mark.parametrize("invalid", ["repeat-means", "fractional-repeats", "shared-fixture", "undeclared-fixture", "missing", "unknown", "duplicate", "nonbinary", "too-few"])
def test_exact_discordance_cannot_approve_ineligible_pairs(invalid):
    cases, results = independent_round(5 if invalid == "too-few" else 70)
    case_id = next(iter(cases))
    repeats = 1
    if invalid == "repeat-means":
        repeats = 2
        results += [replace(result, repeat=2, success=False) for result in results]
    elif invalid == "fractional-repeats":
        repeats = 0.5
    elif invalid == "shared-fixture":
        cases = {key: replace(case, fixture_cluster="one-source-bundle") for key, case in cases.items()}
    elif invalid == "undeclared-fixture":
        cases[case_id] = replace(cases[case_id], fixture_cluster=None)
    elif invalid == "missing":
        results = [result for result in results if (result.case_id, result.arm) != (case_id, "nested")]
    elif invalid == "unknown":
        results = [replace(result, scored=False) if (result.case_id, result.arm) == (case_id, "nested")
                   else result for result in results]
    elif invalid == "duplicate":
        results.append(results[0])
    elif invalid == "nonbinary":
        results[0] = replace(results[0], success=0.5)

    candidate = readout.readout(results, cases, expected_repeats=repeats,
                               decision_method="discordance-exact")["arms"]["nested"]

    assert candidate["verdict"] == readout.INCONCLUSIVE
    assert candidate["comparison"]["interval_95"] == [None, None]
    assert candidate["reading"]
    assert not candidate["passes"]


@pytest.mark.parametrize("writes,passes", [(1, True), (2, False)])
def test_exact_discordance_keeps_the_existing_wrong_write_excess_limit(writes, passes):
    cases, results = independent_round(70, losses=writes)
    affected = set(list(cases)[:writes])
    results = [replace(result, wrong_write=True) if result.arm == "nested" and result.case_id in affected
               else result for result in results]

    candidate = readout.readout(results, cases, expected_repeats=1,
                               decision_method="discordance-exact")["arms"]["nested"]

    assert candidate["verdict"] == readout.PASS
    assert candidate["wrong_write_excess"] == writes
    assert candidate["passes"] is passes


def test_cli_readout_uses_the_frozen_decision_method(tmp_path, monkeypatch, capsys):
    from membench.utility.tool_selection import __main__ as driver

    cases, results = independent_round(9)
    monkeypatch.setattr(driver, "_scored_run", lambda args: (cases, results))
    (tmp_path / "inputs.json").write_text(json.dumps({
        "decision_method": "discordance-exact", "repeats": 1, "arms": ["grouped", "nested"],
    }))

    assert driver.main(["readout", "--run", str(tmp_path)]) == 0
    report = json.loads((tmp_path / "readout.json").read_text())

    assert report["decision_method"] == "discordance-exact"
    assert report["arms"]["nested"]["verdict"] == readout.FAIL
    assert "discordance-exact" in capsys.readouterr().out


def test_run_cli_defaults_to_the_historical_decision_until_explicitly_selected(monkeypatch):
    from membench.utility.tool_selection import __main__ as driver

    selected = []
    monkeypatch.setattr(driver, "cmd_run", lambda args: selected.append(args.decision_method) or 0)
    assert driver.main(["run", "--run", "unused", "--actor-model", "fixture-model"]) == 0
    assert driver.main(["run", "--run", "unused", "--actor-model", "fixture-model",
                        "--decision-method", "discordance-exact"]) == 0
    assert selected == ["grouped-sign-flip", "discordance-exact"]
