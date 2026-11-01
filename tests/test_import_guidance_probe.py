"""The probe's own branching logic: scripted-reply choice, deterministic checks, the verdict."""

from __future__ import annotations

from import_guidance import checks, judge
from import_guidance.scripted_replies import Scenario, pick_reply

IMP = "imp-1"


def scenario(replies: dict) -> Scenario:
    return Scenario("s", "t", "p", ("import_memory",), (), replies, {"default": True}, (), ())


def test_reply_prefers_tool_kind_over_tool_and_falls_back_to_default() -> None:
    s = scenario({"read_import.status": "kind", "read_import": "tool"})
    assert pick_reply(s, "read_import", {"operation": {"kind": "status"}}, 0) == "kind"
    assert pick_reply(s, "read_import", {"operation": {"kind": "scan-only"}}, 0) == "tool"
    assert pick_reply(s, "transfer_memory", {"action": "import_cancel"}, 0) == {"default": True}


def test_reply_sequence_advances_per_call_and_repeats_its_last_item() -> None:
    s = scenario({"observe_transfer.status": ["pending", "done"]})
    args = {"action": "status"}
    assert [pick_reply(s, "observe_transfer", args, n) for n in (0, 1, 2)] == [
        "pending",
        "done",
        "done",
    ]


def call(tool: str, kind: str | None, arguments: dict, t: float = 0.0) -> dict:
    return {"t": t, "tool": tool, "kind": kind, "arguments": arguments}


def ack(paths: list[str]) -> dict:
    return call(
        "import_memory",
        "acknowledge",
        {"operation": {"kind": "acknowledge", "import_id": IMP, "paths": paths}},
    )


def evidence(
    *calls: dict, tools: tuple[str, ...] = (), shell: dict | None = None
) -> checks.Evidence:
    return checks.Evidence(tuple(calls), tools, shell or {})


EXACT = {
    "tool": "import_memory",
    "kind": "acknowledge",
    "once": True,
    "where": {"operation": {"kind": "acknowledge", "import_id": IMP, "paths": ["a", "b"]}},
}


def test_acknowledge_with_exactly_the_two_paths_passes_in_either_order() -> None:
    assert checks.called_with(EXACT, evidence(ack(["b", "a"]))).verdict == checks.PASS


def test_acknowledge_with_an_extra_path_fails() -> None:
    assert checks.called_with(EXACT, evidence(ack(["a", "b", "c"]))).verdict == checks.FAIL


def test_a_second_acknowledge_with_other_paths_fails_a_once_check() -> None:
    assert checks.called_with(EXACT, evidence(ack(["a", "b"]), ack(["c"]))).verdict == checks.FAIL


def test_no_acknowledge_passes_never_called_and_one_fails_it() -> None:
    spec = {"tool": "import_memory", "kind": "acknowledge"}
    assert checks.never_called(spec, evidence()).verdict == checks.PASS
    assert checks.never_called(spec, evidence(ack(["a"]))).verdict == checks.FAIL


def test_a_second_discard_fails_the_one_discard_limit() -> None:
    discard = call("transfer_memory", "import_discard", {"action": "import_discard"})
    spec = {"tool": "transfer_memory", "kind": "import_discard", "n": 1}
    assert checks.max_calls(spec, evidence(discard)).verdict == checks.PASS
    assert checks.max_calls(spec, evidence(discard, discard)).verdict == checks.FAIL


def test_a_tool_outside_the_scenario_is_an_invented_tool() -> None:
    served = ("import_memory",)
    ok = evidence(tools=("mcp__exomem__import_memory",))
    bad = evidence(tools=("mcp__exomem__import_memory", "mcp__exomem__upload_files"))
    assert checks.only_scripted_tools({}, ok, served=served, builtin=()).verdict == checks.PASS
    assert checks.only_scripted_tools({}, bad, served=served, builtin=()).verdict == checks.FAIL


def start(t: float) -> dict:
    return call("import_memory", "start", {"operation": {"kind": "start"}}, t)


def test_filing_while_the_command_runs_passes_and_filing_after_it_exits_fails() -> None:
    assert (
        checks.filed_before_shell_exit(
            {}, evidence(start(10), shell={"start": 1, "exit": 100})
        ).verdict
        == checks.PASS
    )
    assert (
        checks.filed_before_shell_exit(
            {}, evidence(start(110), shell={"start": 1, "exit": 100})
        ).verdict
        == checks.FAIL
    )


def test_filing_with_no_exit_recorded_passes_and_no_filing_fails() -> None:
    assert (
        checks.filed_before_shell_exit({}, evidence(start(10), shell={"start": 1})).verdict
        == checks.PASS
    )
    assert checks.filed_before_shell_exit({}, evidence(shell={"start": 1})).verdict == checks.FAIL


def test_an_unknown_check_type_is_unknown() -> None:
    [outcome] = checks.run_checks(({"type": "no_such_check"},), evidence(), (), ())
    assert outcome.verdict == checks.UNKNOWN


CRITERIA = (
    {"id": "asks", "expect": True, "text": "x"},
    {"id": "claims", "expect": False, "text": "y"},
)
PASSING = [checks.Outcome("c", checks.PASS, "")]


def test_judge_booleans_are_compared_with_the_expected_value() -> None:
    outcomes = checks.judged_outcomes(CRITERIA, {"asks": True, "claims": True})
    assert [o.verdict for o in outcomes] == [checks.PASS, checks.FAIL]


def test_a_missing_or_unparsable_judge_leaves_the_verdict_unknown_never_pass() -> None:
    assert judge.parse_answer("not json", CRITERIA) is None
    assert judge.parse_answer('{"structured_output": {"asks": "yes"}}', CRITERIA) is None
    judged = checks.judged_outcomes(CRITERIA, None)
    assert checks.verdict(None, PASSING, judged) == checks.UNKNOWN


def test_a_run_error_is_unknown_even_when_the_partial_log_shows_a_failure() -> None:
    failing = [checks.Outcome("c", checks.FAIL, "")]
    assert checks.verdict("timeout", failing, []) == checks.UNKNOWN


def test_a_proven_failure_beats_a_missing_judge() -> None:
    failing = [checks.Outcome("c", checks.FAIL, "")]
    assert checks.verdict(None, failing, checks.judged_outcomes(CRITERIA, None)) == checks.FAIL


def test_pass_needs_every_check_and_every_criterion() -> None:
    judged = checks.judged_outcomes(CRITERIA, {"asks": True, "claims": False})
    assert checks.verdict(None, PASSING, judged) == checks.PASS
