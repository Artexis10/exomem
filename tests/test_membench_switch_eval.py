"""The switch evaluation harness: the behaviours whose miss would corrupt the decision.

Each test names the failure only it catches. The sign-flip figures are the
protocol's own (design, "Power", rule 1); the tap and the crash run the real
tap process in front of a stand-in MCP server.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from membench.utility.switch_eval import protocol, stats
from membench.utility.switch_eval.arms import arm_specs
from membench.utility.switch_eval.cases import Case, load_cases
from membench.utility.switch_eval.judge import (
    ITEM_FIELDS,
    JudgeError,
    UnblindingError,
    _refuse_tool_use,
    build_items,
    tie_break,
    unblind,
    write_items,
)
from membench.utility.switch_eval.score import ChainError, decide, rules, score_run
from membench.utility.switch_eval.session import (
    ACTOR_FAILURE,
    HARNESS_FAULT,
    OK,
    AttemptResult,
    PlannedSession,
    RunAborted,
    SessionInputs,
    classify,
    plan_sessions,
    run_session,
)
from membench.utility.switch_eval.state import Layout
from membench.utility.switch_eval.tap import BUDGET_REFUSAL
from membench.utility.switch_eval.tap import __file__ as TAP_SCRIPT

# A stand-in MCP server: answers initialize, tools/list and tools/call; with
# --crash-on-call exits mid-call, as a crashing server under test would, and
# with --exit-at-start exits before it reads a message, as a broken build would.
FAKE_SERVER = """
import json, sys
if "--exit-at-start" in sys.argv:
    sys.exit(2)
crash = "--crash-on-call" in sys.argv
served = 0
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method in ("initialize", "server/discover"):
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "stand-in", "version": "0"}, "instructions": "stand-in"}
    elif method == "tools/list":
        result = {"tools": [{"name": "read_memory", "description": "d", "inputSchema": {"type": "object"}}]}
    else:
        if crash:
            sys.exit(3)
        served += 1
        result = {"content": [{"type": "text", "text": f"served {served}"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
"""


def _start_tap(tmp_path: Path, *server_flags: str) -> tuple[subprocess.Popen, Path, Path]:
    server = tmp_path / "stand_in_server.py"
    server.write_text(FAKE_SERVER, encoding="utf-8")
    log, turn_file = tmp_path / "tap.jsonl", tmp_path / "turn"
    turn_file.write_text("0", encoding="utf-8")
    tap = subprocess.Popen(
        [sys.executable, TAP_SCRIPT, "--log", str(log), "--turn-file", str(turn_file),
         "--budget", str(protocol.PULL_BUDGET_PER_TURN), "--allow", "read_memory",
         "--server-stderr", str(tmp_path / "server.stderr"), "--", sys.executable, str(server), *server_flags],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    return tap, log, turn_file


def _ask(tap: subprocess.Popen, request_id: int, method: str, params: dict) -> dict | None:
    tap.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
    tap.stdin.flush()
    line = tap.stdout.readline()
    return json.loads(line) if line else None


def _log(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _inputs(case_id: str = "case-1", arm: str = "A") -> SessionInputs:
    return SessionInputs(
        planned=PlannedSession(f"{case_id}--{arm}--r1", case_id, arm, 1, 0),
        tree="snapshot",
        argv=("agent-cli", "-p", "--input-format", "stream-json"),
        env={"EXOMEM_STATE_ROOT": "/run/slot/state", "PATH": "/usr/bin"},
        cwd=Path("/run/slot/cwd"),
        turns=("What did we decide about the release?",),
        scored_turn=0,
        files={"instructions.md": "Pull before the answer."},
        state_digests={"vault": "v1", "state": "s1"},
    )


def _session(layout: Layout, case_id: str, arm: str, repeat: int, *attempts: AttemptResult) -> dict:
    """One session's final record through the retry rule, on stand-in attempts."""

    results = iter(attempts)
    inputs = replace(_inputs(case_id, arm), planned=PlannedSession(f"{case_id}--{arm}--r{repeat}", case_id, arm, repeat, 0))
    return run_session(inputs, layout.session_dir(case_id, arm, repeat), lambda inputs, attempt_dir: next(results))


def _case(case_id: str, *, kind: str = "miss", group: str | None = None, expects_memory: bool = True) -> Case:
    return Case(
        case_id=case_id, split=protocol.SPLIT_HELDOUT, kind=kind, group=group or case_id, tree="snapshot",
        turns=("What did we decide about the release?",), scored_turn=0, turn_completeness="complete",
        expects_memory=expects_memory,
    )  # fmt: skip


# -- the statistic ------------------------------------------------------------


@pytest.mark.parametrize(
    ("wins", "losses", "p_value"),
    [(6, 0, 0.015625), (5, 0, 0.03125), (8, 1, 0.01953125)],
)
def test_sign_flip_p_values_match_the_protocols_hand_figures(wins: int, losses: int, p_value: float) -> None:
    # A dozen miss cases scored 0 or 1, each its own group; ties differ by 0.
    differences = [1.0] * wins + [-1.0] * losses + [0.0] * (12 - wins - losses)
    cases = {f"c{n}": d for n, d in enumerate(differences)}
    result = stats.compare(cases, {c: c for c in cases}, seed=0)

    assert result["superiority"]["p_value"] == pytest.approx(p_value)
    assert result["superiority"]["passes"] is (p_value <= protocol.ALPHA_ONE_SIDED)
    # The inverted interval agrees with the test it inverts.
    lower = result["interval_95"][0]
    assert (lower is not None and lower > 0) is result["superiority"]["passes"]


def test_non_inferiority_adds_delta_to_every_case_difference() -> None:
    equal = {f"c{n}": 0.0 for n in range(12)}
    result = stats.compare(equal, {c: c for c in equal}, seed=0, delta=protocol.DELTA_FLOOR)

    # Equal arms: no superiority, but non-inferiority within delta (1 of 4096 patterns).
    assert result["superiority"]["p_value"] == 1.0
    assert result["non_inferiority"]["p_value"] == pytest.approx(1 / 4096)
    # A loss of 0.2 on one of three cases: values -0.1, 0.1, 0.1; 4 of 8 patterns reach 0.1.
    small = stats.compare({"a": -0.2, "b": 0.0, "c": 0.0}, {"a": "a", "b": "b", "c": "c"}, seed=0, delta=0.1)
    assert small["non_inferiority"]["p_value"] == pytest.approx(0.5)


def test_case_differences_sum_within_their_group() -> None:
    differences = {"positive": 1.0, "its-twin": 1.0, "other": 1.0}

    grouped = stats.compare(differences, {"positive": "g1", "its-twin": "g1", "other": "g2"}, seed=0)
    ungrouped = stats.compare(differences, {c: c for c in differences}, seed=0)

    # Two groups (sums 2 and 1) flip as two signs; three cases would flip as three.
    assert grouped["superiority"]["p_value"] == pytest.approx(0.25)
    assert ungrouped["superiority"]["p_value"] == pytest.approx(0.125)
    # The statistic stays the mean over cases, not over groups.
    assert grouped["mean_difference"] == pytest.approx(1.0)


def test_a_run_whose_rules_are_not_evaluable_decides_nothing() -> None:
    # Arms A and P only: every rule needs M, so no rule is evaluable.
    decided = rules(cases=[], table={}, split=protocol.SPLIT_HELDOUT, deltas={2: 0.1, 3: 0.1}, seed=0, arm_ids=["A", "P"])

    assert decide(decided, void=False, split=protocol.SPLIT_HELDOUT).startswith("not evaluable: arms M and A")


def test_rule_1_pools_the_memory_expecting_miss_cases_with_the_memory_expecting_ordinary_turns() -> None:
    # Five miss wins alone cannot pass at 0.025 (p = 1/32); with seven ordinary wins they do.
    cases = [_case(f"miss-{n}") for n in range(5)] + [_case(f"turn-{n}", kind="ordinary") for n in range(7)]
    # Cases that expect no memory, both lost by M, are not rule 1's.
    cases += [_case("miss-none", expects_memory=False), _case("turn-none", kind="ordinary", expects_memory=False)]
    table = {
        c.case_id: {
            "M": {"score": float(c.expects_memory), "contaminated": False},
            "A": {"score": float(not c.expects_memory), "contaminated": False},
        }
        for c in cases
    }

    rule = rules(cases=cases, table=table, split=protocol.SPLIT_HELDOUT, deltas={2: 0.1, 3: 0.1}, seed=0, arm_ids=["A", "M"])["1"]

    assert rule["holds"] and rule["cases"] == 12
    assert rule["M_minus_A"]["superiority"]["p_value"] == pytest.approx(1 / 4096)
    # The miss stratum was selected for A's failure, so the report shows each stratum's share.
    assert {name: (s["cases"], s["share"]) for name, s in rule["strata"].items()} == {
        "miss": (5, pytest.approx(5 / 12)), "ordinary": (7, pytest.approx(7 / 12)),
    }


def test_rule_3_holds_when_m_beats_p_on_quality_though_it_makes_more_calls() -> None:
    cases = [_case(f"turn-{n}", kind="ordinary") for n in range(6)]
    # M pays the activation call that fetches its map; P never makes it.
    table = {c.case_id: {"M": {"score": 1.0, "calls": 3.0}, "P": {"score": 0.0, "calls": 2.0}} for c in cases}

    rule = rules(cases=cases, table=table, split=protocol.SPLIT_HELDOUT, deltas={2: 0.1, 3: 0.1}, seed=0, arm_ids=["M", "P"])["3"]

    assert rule["M_minus_P"]["superiority"]["passes"] and not rule["P_calls_minus_M_calls"]["superiority"]["passes"]
    assert rule["holds"]


def test_a_rule_with_fewer_than_six_groups_reads_as_inconclusive_not_as_failed() -> None:
    # Five winning groups: the smallest sign-flip p-value is 1/32, above 0.025, whatever the data.
    cases = [_case(f"miss-{n}") for n in range(5)]
    table = {c.case_id: {"M": {"score": 1.0, "contaminated": False}, "A": {"score": 0.0, "contaminated": False}} for c in cases}

    decided = rules(cases=cases, table=table, split=protocol.SPLIT_HELDOUT, deltas={2: 0.1, 3: 0.1}, seed=0, arm_ids=["A", "M"])

    assert decided["1"]["inconclusive"]
    assert "inconclusive: rule 1" in decide(decided, void=False, split=protocol.SPLIT_HELDOUT)


def test_a_failed_rule_that_the_power_step_found_short_reads_as_inconclusive_not_as_harm() -> None:
    # M loses three of twelve ordinary turns: rule 2 is not non-inferior within 0.10.
    cases = [_case(f"turn-{n}", kind="ordinary", expects_memory=False) for n in range(12)]
    table = {
        c.case_id: {
            "M": {"score": 0.0 if n < 3 else 1.0, "contaminated": False},
            "A": {"score": 1.0, "contaminated": False},
        }
        for n, c in enumerate(cases)
    }

    decided = rules(cases=cases, table=table, split=protocol.SPLIT_HELDOUT, deltas={2: 0.1, 3: 0.1}, seed=0, arm_ids=["A", "M"], short={2})

    assert not decided["2"]["holds"] and decided["2"]["inconclusive"]
    assert decide(decided, void=False, split=protocol.SPLIT_HELDOUT) != "keep anchors"


# -- the judge's view ---------------------------------------------------------


def test_judge_items_carry_no_arm_marker_and_no_raw_exomem_ref(tmp_path: Path) -> None:
    key = tmp_path / "key.jsonl"
    key.write_text(
        json.dumps({
            "case_id": "case-1", "kind": "miss", "group": "case-1", "scored_turn": 0,
            "turn_completeness": "complete", "expects_memory": True,
            "required": [{"item": "release-freeze", "statement": "The release is frozen until the audit closes.",
                          "source_refs": ["Knowledge Base/Decisions/Release.md"]}],
            "forbidden": [{"item": "old-date", "claim": "The release ships on the first.", "why": "superseded",
                           "source_refs": []}],
            "successors": {}, "excluded_refs": [], "struck": None,
        }) + "\n",
        encoding="utf-8",
    )  # fmt: skip
    turns = tmp_path / "cases.jsonl"
    turns.write_text(
        json.dumps({"case_id": "case-1", "turns": ["Check exomem://page/release and tell me the plan."]}) + "\n",
        encoding="utf-8",
    )
    (case,), _ = load_cases(keys=[("heldout", key)], case_files=[turns])
    answer = "The release is frozen until the audit closes (see exomem://page/release)."

    def answered(inputs: SessionInputs, attempt_dir: Path) -> AttemptResult:
        return AttemptResult(OK, answer=answer)

    sessions = [
        run_session(_inputs("case-1", arm), tmp_path / "sessions" / arm, answered)
        for arm in protocol.ARM_IDS
    ]
    layout = Layout(tmp_path / "run")
    write_items(layout, build_items([case], sessions, seed=7, inputs_digest="inputs"))

    items = [json.loads(line) for line in (layout.judge / "items.jsonl").read_text(encoding="utf-8").splitlines()]
    originals = [item for item in items if item["item_id"] in json.loads((layout.unblinding / "arm-key.json").read_text())]
    assert all(tuple(sorted(item)) == tuple(sorted(ITEM_FIELDS)) for item in items)
    # The same answer under every arm gives the same view: nothing marks the arm.
    views = {json.dumps({k: v for k, v in item.items() if k != "item_id"}, sort_keys=True) for item in originals}
    assert len(views) == 1
    judge_text = "".join(path.read_text(encoding="utf-8") for path in layout.judge.rglob("*") if path.is_file())
    assert "exomem://" not in judge_text
    assert not any(s["session_id"] in judge_text for s in sessions)
    assert not list(layout.judge.rglob("arm-key*"))


# -- the pull budget ----------------------------------------------------------


def test_an_over_budget_call_is_refused_and_counted(tmp_path: Path) -> None:
    tap, log, turn_file = _start_tap(tmp_path)
    _ask(tap, 1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    replies = [
        _ask(tap, 10 + n, "tools/call", {"name": "read_memory", "arguments": {"ref": "x"}})
        for n in range(protocol.PULL_BUDGET_PER_TURN + 1)
    ]
    turn_file.write_text("1", encoding="utf-8")
    next_turn = _ask(tap, 99, "tools/call", {"name": "read_memory", "arguments": {"ref": "x"}})
    tap.stdin.close()
    tap.wait(timeout=30)

    over = replies[-1]["result"]
    assert over.get("isError") is True and over["content"][0]["text"] == BUDGET_REFUSAL
    assert [r["result"]["content"][0]["text"] for r in replies[:-1]] == [
        f"served {n}" for n in range(1, protocol.PULL_BUDGET_PER_TURN + 1)
    ]
    calls = [e for e in _log(log) if e["event"] == "call" and e["turn"] == 0]
    assert len(calls) == protocol.PULL_BUDGET_PER_TURN + 1
    assert [c["outcome"] for c in calls].count("refused_budget") == 1
    # The budget is per user turn: the next turn's first call reaches the server.
    assert next_turn["result"]["content"][0]["text"] == f"served {protocol.PULL_BUDGET_PER_TURN + 1}"


# -- the retry rule -----------------------------------------------------------


def test_a_harness_fault_is_retried_with_identical_inputs(tmp_path: Path) -> None:
    received: list[SessionInputs] = []
    outcomes = iter([HARNESS_FAULT, HARNESS_FAULT, OK])

    def flaky(inputs: SessionInputs, attempt_dir: Path) -> AttemptResult:
        received.append(inputs)
        outcome = next(outcomes)
        return AttemptResult(outcome, "rate_limit" if outcome == HARNESS_FAULT else "", answer="An answer.")

    inputs = _inputs()
    record = run_session(inputs, tmp_path / "flaky", flaky)

    assert record["outcome"] == OK and record["attempts"] == 1 + protocol.HARNESS_FAULT_RETRIES
    assert all(r is inputs for r in received)
    attempts = [json.loads(line) for line in (tmp_path / "flaky" / "attempts.jsonl").read_text().splitlines()]
    assert {a["inputs_digest"] for a in attempts} == {inputs.digest()}

    def always_faulting(inputs: SessionInputs, attempt_dir: Path) -> AttemptResult:
        return AttemptResult(HARNESS_FAULT, "transport")

    struck = run_session(_inputs("case-2"), tmp_path / "faulting", always_faulting)
    assert struck["outcome"] == HARNESS_FAULT and struck["attempts"] == 1 + protocol.HARNESS_FAULT_RETRIES


def test_a_case_id_that_climbs_out_of_the_run_directory_still_writes_inside_it(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    (planned,) = plan_sessions([_case("../../escaped-case")], arm_ids=["A"], repeats=1, seed=0)

    session_dir = layout.session_dir(planned.case_id, planned.arm, planned.repeat)
    run_session(_inputs(planned.case_id), session_dir, lambda inputs, attempt_dir: AttemptResult(OK, answer="An answer."))

    (written,) = tmp_path.rglob("session.json")
    assert written.resolve().is_relative_to(layout.sessions.resolve())


def test_scoring_refuses_a_run_whose_failed_repeat_lost_its_record(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    sessions = {}
    for arm in ("A", "M"):
        for repeat in (1, 2, 3):
            timed_out = arm == "M" and repeat == 3
            attempt = AttemptResult(ACTOR_FAILURE, "timeout") if timed_out else AttemptResult(OK, answer="An answer.")
            record = _session(layout, "case-1", arm, repeat, attempt)
            sessions[record["session_id"]] = record
    verdicts = {
        session_id: {"required": {}, "forbidden": {}, "contaminated": None}
        for session_id, record in sessions.items()
        if record["outcome"] == OK
    }
    # M's timed-out repeat has no final record: dropping it would lift M's score from 2/3 to 1.
    del sessions["case-1--M--r3"]

    with pytest.raises(ChainError):
        score_run(
            layout, cases=[_case("case-1")], sessions=sessions, verdicts=verdicts, arm_ids=["A", "M"],
            repeats=3, split=protocol.SPLIT_HELDOUT, deltas={2: 0.1, 3: 0.1}, seed=0,
        )  # fmt: skip


# The stateless handshake is what the pinned client sends on the 2026-07-28 protocol.
@pytest.mark.parametrize("handshake", ["initialize", "server/discover"])
def test_a_crash_of_the_server_under_test_is_an_actor_failure_and_not_retried(tmp_path: Path, handshake: str) -> None:
    tap, log, _turn_file = _start_tap(tmp_path, "--crash-on-call")
    _ask(tap, 1, handshake, {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}})
    _ask(tap, 2, "tools/call", {"name": "read_memory", "arguments": {}})
    tap.wait(timeout=30)
    tap_events = _log(log)
    calls: list[int] = []

    def crashed(inputs: SessionInputs, attempt_dir: Path) -> AttemptResult:
        calls.append(1)
        return classify(
            spec=arm_specs()["P"], model="m", client_version="v", events=[], turn_records=[],
            tap_events=tap_events, scored_turn=0, timed_out=False, quota=None, surface=None,
        )  # fmt: skip

    record = run_session(_inputs(), tmp_path / "crashed", crashed)

    assert (record["outcome"], record["kind"]) == (ACTOR_FAILURE, "server_crash")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "server_starts",
    [True, False],
    # A launcher that refuses to deliver the credential starts neither the
    # client nor the server: no init event, no tap log.
    ids=["a server that exits before initialize", "a client that never reports its session"],
)
def test_a_session_that_never_starts_is_retried_then_stops_the_run(tmp_path: Path, server_starts: bool) -> None:
    tap_events: list[dict] = []
    if server_starts:
        tap, log, _turn_file = _start_tap(tmp_path, "--exit-at-start")
        tap.wait(timeout=30)
        tap_events = _log(log)
    calls: list[int] = []

    def never_started(inputs: SessionInputs, attempt_dir: Path) -> AttemptResult:
        calls.append(1)
        return classify(
            spec=arm_specs()["P"], model="m", client_version="v", events=[], turn_records=[],
            tap_events=tap_events, scored_turn=0, timed_out=False, quota=None, surface=None,
        )  # fmt: skip

    with pytest.raises(RunAborted):
        run_session(_inputs(), tmp_path / "never-started", never_started)

    assert len(calls) == 1 + protocol.HARNESS_FAULT_RETRIES
    assert not (tmp_path / "never-started" / "session.json").exists()


@pytest.mark.parametrize(
    "leak",
    [
        # Otherwise the arm's own surface, so only the extra server is wrong.
        {"type": "system", "subtype": "init", "apiKeySource": "none", "plugins": [],
         "model": "m", "claude_code_version": "v",
         "tools": [f"mcp__exomem__{tool}" for tool in protocol.READ_TOOLS],
         "mcp_servers": [{"name": "exomem", "status": "connected"}, {"name": "other", "status": "connected"}]},
        {"type": "system", "subtype": "hook_started", "hook_id": "h1", "hook_name": "SessionStart:startup",
         "hook_event": "SessionStart"},
    ],
    ids=["an MCP server the arm does not name", "a hook the arm does not wire"],
)  # fmt: skip
def test_a_client_surface_beyond_the_arm_stops_the_run(leak: dict) -> None:
    result = classify(
        spec=arm_specs()["A"], model="m", client_version="v", events=[leak], turn_records=[],
        tap_events=[], scored_turn=0, timed_out=False, quota=None, surface=None,
    )  # fmt: skip

    assert result.outcome == "abort"


def test_a_judge_that_ran_a_tool_gives_no_verdict() -> None:
    # The `codex exec --json` shapes: a read-only shell still reads the arm key.
    answered = json.dumps({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "{}"}})
    looked = json.dumps({"type": "item.started", "item": {"id": "item_1", "type": "command_execution", "command": "cat ../arm-key.json"}})  # fmt: skip

    _refuse_tool_use(answered)
    with pytest.raises(JudgeError):
        _refuse_tool_use(looked + "\n" + answered)


# -- unblinding and the tie-break ---------------------------------------------


def test_the_tie_break_needs_the_arm_key_and_runs_only_after_unblinding(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    layout.judge.mkdir(parents=True)
    layout.unblinding.mkdir(parents=True)
    (layout.judge / "items.jsonl").write_text(
        "".join(json.dumps({"item_id": i}) + "\n" for i in ("item-m", "item-a")), encoding="utf-8"
    )
    first = {"required": {"r1": "used"}, "forbidden": {"f1": "absent"}, "contaminated": None}
    second = {"required": {"r1": "missing"}, "forbidden": {"f1": "asserted_as_current"}, "contaminated": None}
    for route, verdict in (("codex", first), ("claude", second)):
        (layout.judge / "verdicts" / route).mkdir(parents=True)
        for item_id in ("item-m", "item-a"):
            (layout.judge / "verdicts" / route / f"{item_id}.json").write_text(json.dumps({**verdict, "item_id": item_id}))

    with pytest.raises(UnblindingError):
        unblind(layout, routes=["codex", "claude"])  # no arm key yet
    with pytest.raises(UnblindingError):
        tie_break({**first, "item_id": "item-m"}, {**second, "item_id": "item-m"}, unblinded={"item-m": {"arm": "M"}})

    key = {
        "item-m": {"session_id": "s-m", "case_id": "c", "arm": "M", "repeat": 1, "rejudge_of": None},
        "item-a": {"session_id": "s-a", "case_id": "c", "arm": "A", "repeat": 1, "rejudge_of": None},
    }
    (layout.unblinding / "arm-key.json").write_text(json.dumps(key), encoding="utf-8")
    (layout.judge / "verdicts" / "claude" / "item-a.json").unlink()
    with pytest.raises(UnblindingError):
        unblind(layout, routes=["codex", "claude"])  # the second judge has not judged every item
    (layout.judge / "verdicts" / "claude" / "item-a.json").write_text(json.dumps({**second, "item_id": "item-a"}))
    unblinded = unblind(layout, routes=["codex", "claude"])

    # Where the judges disagree, the verdict that weighs against the switch stands.
    for_m = tie_break({**first, "item_id": "item-m"}, {**second, "item_id": "item-m"}, unblinded=unblinded)
    for_a = tie_break({**first, "item_id": "item-a"}, {**second, "item_id": "item-a"}, unblinded=unblinded)
    assert (for_m["required"]["r1"], for_m["forbidden"]["f1"]) == ("missing", "asserted_as_current")
    assert (for_a["required"]["r1"], for_a["forbidden"]["f1"]) == ("used", "absent")
