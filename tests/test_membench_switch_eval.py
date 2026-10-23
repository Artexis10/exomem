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
from membench.utility.switch_eval.audit import (
    load_audit,
    load_guesses,
    rejudge_set,
    run_guesses,
    write_rejudge_set,
)
from membench.utility.switch_eval.cases import Case, Required, load_cases
from membench.utility.switch_eval.judge import (
    ITEM_FIELDS,
    JudgeError,
    Unblinded,
    UnblindingError,
    _refuse_tool_use,
    build_items,
    exomem_tool_names,
    item_payload,
    record_first_route,
    run_judge,
    tie_break,
    unblind,
    validate_verdict,
    write_items,
)
from membench.utility.switch_eval.score import ChainError, decide, read_power, rules, score_run
from membench.utility.switch_eval.session import (
    ACTOR_FAILURE,
    HARNESS_FAULT,
    OK,
    AttemptResult,
    ClaudeSessionExecutor,
    PlannedSession,
    RunAborted,
    SessionInputs,
    classify,
    plan_sessions,
    run_session,
)
from membench.utility.switch_eval.state import Layout, Slot
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
probes = 0
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
    elif method == "tools/call" and message["params"]["name"] == "ask_memory":
        if "--check-meta" in sys.argv:
            assert message["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        probes += 1
        if "--never-ready" in sys.argv or ("--warming-once" in sys.argv and probes == 1):
            result = {"structuredContent": {"result": {"success": False,
                      "error": {"code": "RETRIEVAL_INDEX_WARMING"}}}}
        else:
            result = {"structuredContent": {"result": {"hits": [],
                      "warming": {"components": ["embeddings", "graph", "recall_projection"], "since_s": None}}}}
    else:
        if crash:
            sys.exit(3)
        served += 1
        result = {"content": [{"type": "text", "text": f"served {served}"}]}
        if "--read-result" in sys.argv:
            result = json.loads(sys.argv[sys.argv.index("--read-result") + 1])
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


def _case(
    case_id: str, *, kind: str = "miss", group: str | None = None, expects_memory: bool = True,
    required: tuple[Required, ...] = (),
) -> Case:
    return Case(
        case_id=case_id, split=protocol.SPLIT_HELDOUT, kind=kind, group=group or case_id, tree="snapshot",
        turns=("What did we decide about the release?",), scored_turn=0, turn_completeness="complete",
        expects_memory=expects_memory, required=required,
    )  # fmt: skip


def _write_verdict(layout: Layout, route: str, item_id: str, required: dict[str, str]) -> None:
    """A stand-in for ``route`` judging ``item_id``; as in a judge run, the first route to give a verdict is recorded."""
    folder = layout.judge / "verdicts" / route
    folder.mkdir(parents=True, exist_ok=True)
    verdict = {"item_id": item_id, "required": required, "forbidden": {}, "contaminated": None, "span": None}
    (folder / f"{item_id}.json").write_text(json.dumps(verdict), encoding="utf-8")
    record_first_route(layout, route)


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


def _dev(kind: str, repeats: dict[str, tuple[float, ...]], calls: dict[str, tuple[float, ...]] | None = None) -> stats.DevCase:
    return stats.DevCase(kind=kind, expects_memory=True, repeats=repeats, calls=calls or {arm: (2.0, 2.0, 2.0) for arm in repeats})


def test_the_power_step_simulates_the_frozen_groups_not_one_group_per_case() -> None:
    # Twelve held-out ordinary turns in two frozen groups: two groups never reach 0.025.
    design = [stats.DesignCase("ordinary", f"g{n % 2}", True) for n in range(12)]
    # Repeats that never vary: equal arms then differ by exactly 0, so each case alone would pass.
    dev = [_dev("ordinary", {arm: (1.0, 1.0, 1.0) for arm in "AMP"})] * 4

    rule = stats.power_step(design, dev, reserve=[], seed=0, simulations=200)["rules"]["2"]

    assert rule["groups"] == 2
    assert rule["pass_rate"] == 0.0 and rule["short"]


def test_the_power_step_keeps_each_cases_repeat_spread_instead_of_a_pooled_rate() -> None:
    design = [stats.DesignCase("ordinary", f"turn-{n}", True) for n in range(12)]
    # Both pools pass one repeat in three; only the second varies between repeats.
    stable = [_dev("ordinary", {"A": (0.0, 0.0, 0.0), "M": (1.0, 1.0, 1.0), "P": (0.0, 0.0, 0.0)})] * 4
    variable = [_dev("ordinary", {arm: (1.0, 0.0, 0.0) for arm in "AMP"})] * 4

    def first_rate(dev: list[stats.DevCase]) -> float:
        return stats.power_step(design, dev, reserve=[], seed=0, simulations=200)["rules"]["2"]["table"][0]["pass_rate"]

    assert first_rate(stable) == 1.0
    assert first_rate(variable) < 0.8


def test_the_power_step_simulates_rule_3s_calls_test_from_the_development_call_counts() -> None:
    design = [stats.DesignCase("ordinary", f"turn-{n}", True) for n in range(12)]
    scores = {arm: (1.0, 1.0, 1.0) for arm in "AMP"}
    # In development M makes one call where P makes three: M wins the calls test.
    dev = [_dev("ordinary", scores, calls={"A": (2.0,) * 3, "M": (1.0,) * 3, "P": (3.0,) * 3})] * 4

    rule = stats.power_step(design, dev, reserve=[], seed=0, simulations=200)["rules"]["3"]

    assert rule["calls_pass_rate"] == 1.0


def test_the_power_step_counts_reserve_turns_in_their_keyed_groups() -> None:
    # Six held-out ordinary turns share one group, and the five keyed reserve turns join it.
    design = [stats.DesignCase("ordinary", "g", True) for _ in range(6)]
    reserve = [stats.DesignCase("ordinary", "g", True) for _ in range(5)]
    dev = [_dev("ordinary", {arm: (1.0, 1.0, 1.0) for arm in "AMP"})] * 4

    rule = stats.power_step(design, dev, reserve=reserve, seed=0, simulations=200)["rules"]["2"]

    # As singleton groups the reserve would read as six groups that pass every simulation.
    assert rule["groups"] == 1 and rule["short"]


def test_rule_3s_power_is_short_when_ms_activation_call_makes_the_composite_rule_unreachable() -> None:
    design = [stats.DesignCase("ordinary", f"turn-{n}", True) for n in range(12)]
    scores = {arm: (1.0, 1.0, 1.0) for arm in "AMP"}
    # Equal quality, so only non-inferiority with fewer calls could pass, and M makes one call more than P.
    dev = [_dev("ordinary", scores, calls={"A": (3.0,) * 3, "M": (3.0,) * 3, "P": (2.0,) * 3})] * 4

    rule = stats.power_step(design, dev, reserve=[], seed=0, simulations=200)["rules"]["3"]

    assert rule["short"]


def test_scoring_refuses_power_figures_computed_for_another_design(tmp_path: Path) -> None:
    dev = [_dev("ordinary", {arm: (1.0, 0.0, 1.0) for arm in "AMP"})] * 4
    build = {"harness_digest": "harness-1", "arm_hashes": {arm: f"arm-{arm}" for arm in "AMP"}}
    power = tmp_path / "power.json"
    simulated = [stats.DesignCase("ordinary", "turn-0", True)]
    power.write_text(json.dumps({**stats.power_step(simulated, dev, reserve=[], seed=0, simulations=50), "build": build}))
    scored = [stats.DesignCase("ordinary", f"turn-{n}", True) for n in range(7)]

    # One simulated turn says rules 2 and 3 are short at the cap; seven scored turns are another experiment.
    with pytest.raises(ChainError):
        read_power(power, design=scored, build=build)


def test_a_run_whose_rules_are_not_evaluable_decides_nothing() -> None:
    # Arms A and P only: every rule needs M, so no rule is evaluable.
    decided = rules(cases=[], table={}, split=protocol.SPLIT_HELDOUT, deltas={2: 0.1, 3: 0.1}, seed=0, arm_ids=["A", "P"])

    assert decide(decided, void=False, split=protocol.SPLIT_HELDOUT, audited=True).startswith("not evaluable: arms M and A")


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
    assert "inconclusive: rule 1" in decide(decided, void=False, split=protocol.SPLIT_HELDOUT, audited=True)


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
    assert decide(decided, void=False, split=protocol.SPLIT_HELDOUT, audited=True) != "keep anchors"


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
    write_items(layout, build_items([case], sessions, seed=7, inputs_digest="inputs", tool_names=()))

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


def test_the_judge_view_masks_every_arms_tool_names_in_an_answer_from_any_arm(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    # What the state stage captured from the server for each arm: P's list lacks activate_context.
    for arm, tools in (("A", ["activate_context", "read_memory"]), ("P", ["read_memory"])):
        surface = {"shown": {"initialize": {"tools": [{"name": name} for name in tools]}}}
        layout.arm_tree(arm, "snapshot").mkdir(parents=True)
        (layout.arm_tree(arm, "snapshot") / "surface.json").write_text(json.dumps(surface), encoding="utf-8")
        (layout.arm_dir(arm) / "session-inputs").mkdir(parents=True)
        (layout.arm_dir(arm) / "session-inputs" / "mcp.json").write_text(json.dumps({"mcpServers": {"exomem": {}}}), encoding="utf-8")
    p_answer = "I had no activate_context, so mcp__exomem__read_memory and read_memory did it. The release is frozen."

    names = exomem_tool_names(layout, arms=["A", "P"], trees=["snapshot"])
    item = item_payload(_case("case-1"), p_answer, "item-1", tool_names=names)

    assert item["answer"] == "I had no [tool], so [tool] and [tool] did it. The release is frozen."


def test_a_contamination_verdict_without_its_span_is_refused() -> None:
    item = item_payload(_case("case-1", expects_memory=False), "You said the release ships Friday.", "item-1", tool_names=())
    verdict = {"item_id": "item-1", "required": [], "forbidden": [], "no_memory": {"asserts_unsupplied_user_facts": True, "span": None}}

    with pytest.raises(JudgeError):
        validate_verdict(item, verdict)


# -- the pull budget ----------------------------------------------------------


@pytest.mark.parametrize("eventually_ready", [True, False], ids=["ready", "setup-timeout"])
def test_the_ready_gate_holds_requests_without_spending_the_turn_budget_or_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, eventually_ready: bool) -> None:
    from membench.utility.switch_eval import session

    slot = Slot(tmp_path / "slot")
    slot.runtime.mkdir(parents=True)
    slot.cwd.mkdir()
    server = tmp_path / "stand_in_server.py"
    server.write_text(FAKE_SERVER, encoding="utf-8")
    client = tmp_path / "stand_in_client.py"
    client.write_text('''
import json, subprocess, sys
request = json.loads(sys.stdin.readline())
tap = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
def ask(rid, method, params):
    tap.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\\n")
    tap.stdin.flush()
    reply = json.loads(tap.stdout.readline())
    assert reply["id"] == rid
    return reply
meta = {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}
ask(1, "server/discover", meta)
ask(2, "tools/list", meta)
print(json.dumps({"type": "system", "subtype": "init", "apiKeySource": "none",
      "model": "m", "claude_code_version": "v", "tools": ["mcp__exomem__read_memory"],
      "mcp_servers": [{"name": "exomem", "status": "connected"}]}), flush=True)
reply = ask(3, "tools/call", {"name": "read_memory", "arguments": {}})
print(json.dumps({"type": "result", "subtype": "success",
      "result": reply["result"]["content"][0]["text"]}), flush=True)
tap.stdin.close()
tap.wait(timeout=10)
''', encoding="utf-8")
    monkeypatch.setattr(session, "prepare_session_slot", lambda *args: slot)
    spec = replace(arm_specs()["P"], tools=("read_memory",))
    inputs = replace(_inputs(arm="P"), cwd=slot.cwd, argv=(
        sys.executable, str(client), sys.executable, TAP_SCRIPT,
        "--log", str(slot.runtime / "tap.jsonl"), "--turn-file", str(slot.turn_file),
        "--budget", "1", "--allow", "read_memory",
        "--ready-timeout", "5" if eventually_ready else "0.05",
        "--server-stderr", str(slot.runtime / "server.stderr"),
        "--", sys.executable, str(server), "--check-meta",
        "--warming-once" if eventually_ready else "--never-ready",
    ))
    # Warm-up lasts a second; an actor turn gets only half a second.
    executor = ClaudeSessionExecutor(Layout(tmp_path), {"P": spec}, "m", "v", {}, turn_timeout=0.5)
    result = executor(inputs, tmp_path / "attempt")

    tap_events = _log(tmp_path / "attempt" / "runtime" / "tap.jsonl")
    ready = next(e for e in tap_events if e["event"] == "ready_finished")
    if not eventually_ready:
        assert result.outcome == HARNESS_FAULT and result.kind == "recall_not_ready"
        assert ready["outcome"] == "timeout"
        assert not any(e["event"] == "call" for e in tap_events)
        return

    assert result.outcome == OK and result.answer == "served 1"
    assert ready["outcome"] == "ready" and ready["probes"] == 2
    assert next(e["t"] for e in tap_events if e["event"] == "tools_list") >= ready["t"]
    turn = result.turns[0]
    assert turn["calls"] == 1 and turn["calls_refused_budget"] == 0
    assert turn["ready_wait_seconds"] >= 1.0 and turn["wall_seconds"] < 0.5


@pytest.mark.parametrize("refusal", [
    {"structuredContent": {"abstained": True, "abstention": {"reason": "index_warming"}}},
    {"content": [{"type": "text", "text": json.dumps({"success": False, "error": {"code": "RETRIEVAL_INDEX_WARMING"}})}]},
])
def test_a_warming_read_after_the_gate_is_a_harness_fault(tmp_path: Path, refusal: dict) -> None:
    tap, log, _turn_file = _start_tap(tmp_path, "--read-result", json.dumps(refusal))
    _ask(tap, 1, "initialize", {})
    _ask(tap, 2, "tools/call", {"name": "read_memory", "arguments": {}})
    tap.stdin.close()
    tap.wait(timeout=30)

    def warming(inputs: SessionInputs, attempt_dir: Path) -> AttemptResult:
        return classify(
            spec=arm_specs()["P"], model="m", client_version="v", events=[], turn_records=[],
            tap_events=_log(log), scored_turn=0, timed_out=True, quota=None, surface=None,
        )

    record = run_session(_inputs(), tmp_path / "warming", warming)
    assert record["outcome"] == HARNESS_FAULT and record["kind"] == "recall_warming"
    assert record["attempts"] == 1 + protocol.HARNESS_FAULT_RETRIES


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


def test_usage_and_the_failure_report_read_every_attempt_not_only_the_last(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    turn = {"calls": 1, "activation_calls": 0, "input_tokens": 100, "output_tokens": 10, "push_bytes": 0,
            "wall_seconds": 1.0, "hook_responses": []}  # fmt: skip
    record = _session(
        layout, "case-1", "A", 1,
        AttemptResult(HARNESS_FAULT, "rate_limit", turns=[turn]),
        AttemptResult(HARNESS_FAULT, "transport", turns=[turn]),
        AttemptResult(OK, answer="An answer.", turns=[turn]),
    )  # fmt: skip
    verdict = {"required": {}, "forbidden": {}, "contaminated": None}

    scored = score_run(
        layout, cases=[_case("case-1")], sessions={record["session_id"]: record}, verdicts={record["session_id"]: verdict},
        arm_ids=["A"], repeats=1, split=protocol.SPLIT_HELDOUT, deltas={2: 0.1, 3: 0.1}, seed=0,
    )  # fmt: skip

    assert scored["usage"]["totals"] == {"input_tokens": 300, "output_tokens": 30, "wall_seconds": 3.0, "attempts": 3}
    assert scored["secondary"]["A"]["harness_faults_by_kind"] == {"rate_limit": 1, "transport": 1}


def test_usage_and_retried_faults_read_as_unavailable_when_an_attempt_log_is_missing(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    turn = {"calls": 1, "activation_calls": 0, "input_tokens": 100, "output_tokens": 10, "push_bytes": 0,
            "wall_seconds": 1.0, "hook_responses": []}  # fmt: skip
    record = _session(layout, "case-1", "A", 1, AttemptResult(HARNESS_FAULT, "rate_limit", turns=[turn]),
                      AttemptResult(OK, answer="An answer.", turns=[turn]))  # fmt: skip
    (layout.session_dir("case-1", "A", 1) / "attempts.jsonl").unlink()
    verdict = {"required": {}, "forbidden": {}, "contaminated": None}

    scored = score_run(
        layout, cases=[_case("case-1")], sessions={record["session_id"]: record}, verdicts={record["session_id"]: verdict},
        arm_ids=["A"], repeats=1, split=protocol.SPLIT_HELDOUT, deltas={2: 0.1, 3: 0.1}, seed=0,
    )  # fmt: skip

    # The final record still names two attempts; zero would understate the run.
    assert scored["usage"]["totals"] is None
    assert scored["secondary"]["A"]["harness_faults_by_kind"] is None


def test_a_skipped_struck_session_counts_no_usage_instead_of_making_usage_unavailable(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    turn = {"calls": 1, "activation_calls": 0, "input_tokens": 100, "output_tokens": 10, "push_bytes": 0,
            "wall_seconds": 1.0, "hook_responses": []}  # fmt: skip
    record = _session(layout, "case-1", "A", 1, AttemptResult(OK, answer="An answer.", turns=[turn]))
    # The runner's record for a session it skipped because its case was struck: no attempt ran, so no log exists.
    skipped = {"session_id": "case-2--A--r1", "case_id": "case-2", "arm": "A", "repeat": 1, "outcome": "struck",
               "kind": "case struck after harness faults", "attempts": 0, "turns": [], "answer": None, "checks": {}}  # fmt: skip
    verdict = {"required": {}, "forbidden": {}, "contaminated": None}

    scored = score_run(
        layout, cases=[_case("case-1"), _case("case-2")], sessions={record["session_id"]: record, skipped["session_id"]: skipped},
        verdicts={record["session_id"]: verdict}, arm_ids=["A"], repeats=1, split=protocol.SPLIT_HELDOUT,
        deltas={2: 0.1, 3: 0.1}, seed=0,
    )  # fmt: skip

    assert scored["usage"]["totals"] == {"input_tokens": 100, "output_tokens": 10, "wall_seconds": 1.0, "attempts": 1}


def test_the_activation_share_counts_user_turns_with_an_activate_context_call_not_calls(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    def turn(calls: int, activation_calls: int) -> dict:
        return {"calls": calls, "activation_calls": activation_calls, "input_tokens": 0, "output_tokens": 0,
                "push_bytes": 0, "wall_seconds": 0.0, "hook_responses": []}  # fmt: skip
    # M obeys the reminder on one of its two user turns, and activates twice there.
    record = _session(layout, "case-1", "M", 1, AttemptResult(OK, answer="An answer.", turns=[turn(3, 2), turn(1, 0)]))
    verdict = {"required": {}, "forbidden": {}, "contaminated": None}

    scored = score_run(
        layout, cases=[_case("case-1")], sessions={record["session_id"]: record}, verdicts={record["session_id"]: verdict},
        arm_ids=["M"], repeats=1, split=protocol.SPLIT_HELDOUT, deltas={2: 0.1, 3: 0.1}, seed=0,
    )  # fmt: skip

    assert scored["secondary"]["M"]["activation_share"] == {"user_turns": 2, "with_activation": 1, "share": 0.5}


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


def test_scoring_on_one_judge_is_refused_when_its_agreement_calls_for_recovery(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    case = _case("case-1", required=(Required("release-freeze", "The release is frozen."),))
    sessions = [_session(layout, "case-1", arm, 1, AttemptResult(OK, answer="The release is frozen.")) for arm in protocol.ARM_IDS]
    item_set = build_items([case], sessions, seed=7, inputs_digest="inputs", tool_names=())
    write_items(layout, item_set)
    # The judge contradicts itself on every re-judged copy: required-item agreement is 0.
    for item_id, entry in item_set.arm_key.items():
        _write_verdict(layout, "codex", item_id, {"r1": "missing" if entry["rejudge_of"] else "used"})

    with pytest.raises(UnblindingError, match="recovery"):
        unblind(layout, routes=["codex"], expected=item_set.arm_key)


def test_scoring_on_the_recovery_route_alone_is_refused_once_recovery_was_required(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    case = _case("case-1", required=(Required("release-freeze", "The release is frozen."),))
    sessions = [_session(layout, "case-1", arm, 1, AttemptResult(OK, answer="The release is frozen.")) for arm in protocol.ARM_IDS]
    item_set = build_items([case], sessions, seed=7, inputs_digest="inputs", tool_names=())
    write_items(layout, item_set)
    # Codex judged first and contradicts itself on every re-judged copy; Claude then judged every item.
    for item_id, entry in item_set.arm_key.items():
        _write_verdict(layout, "codex", item_id, {"r1": "missing" if entry["rejudge_of"] else "used"})
    for item_id in item_set.arm_key:
        _write_verdict(layout, "claude", item_id, {"r1": "used"})

    # Dropping the noisy first judge would skip the tie-break that recovery prescribes.
    with pytest.raises(UnblindingError):
        unblind(layout, routes=["claude"], expected=item_set.arm_key)


# A stand-in for a judge client whose quota is spent before it gives a verdict.
SPENT_JUDGE = """#!{python}
import sys
sys.stderr.write("usage limit reached\\n")
sys.exit(1)
"""


def test_a_judge_route_spent_before_its_first_verdict_leaves_the_fallback_route_to_stand_alone(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    sessions = [_session(layout, "case-1", arm, 1, AttemptResult(OK, answer="An answer.")) for arm in protocol.ARM_IDS]
    item_set = build_items([_case("case-1")], sessions, seed=7, inputs_digest="inputs", tool_names=())
    write_items(layout, item_set)
    spent = tmp_path / "client" / "judge"
    spent.parent.mkdir()
    spent.write_text(SPENT_JUDGE.format(python=sys.executable), encoding="utf-8")
    spent.chmod(0o755)

    # The Claude route gives no verdict; the Codex route then judges every item.
    with pytest.raises(JudgeError):
        run_judge(layout, route="claude", model="stand-in", effort=None, claude=str(spent), launcher=())
    for item_id in item_set.arm_key:
        _write_verdict(layout, "codex", item_id, {})

    assert unblind(layout, routes=["codex"], expected=item_set.arm_key).routes == ("codex",)


def test_unblinding_refuses_an_arm_key_that_names_another_arm_for_a_session(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    sessions = [_session(layout, "case-1", arm, 1, AttemptResult(OK, answer="An answer.")) for arm in protocol.ARM_IDS]
    item_set = build_items([_case("case-1")], sessions, seed=7, inputs_digest="inputs", tool_names=())
    write_items(layout, item_set)
    for item_id in item_set.arm_key:
        _write_verdict(layout, "codex", item_id, {})
    stored = json.loads((layout.unblinding / "arm-key.json").read_text(encoding="utf-8"))
    # M's item now claims arm A while it keeps M's session: the tie-break would favour it.
    m_item = next(item_id for item_id, entry in stored.items() if entry["arm"] == "M")
    stored[m_item]["arm"] = "A"
    (layout.unblinding / "arm-key.json").write_text(json.dumps(stored), encoding="utf-8")

    with pytest.raises(UnblindingError):
        unblind(layout, routes=["codex"], expected=item_set.arm_key)


# A stand-in for the audit lane's client: records the item each prompt shows and guesses A.
STAND_IN_GUESSER = """#!{python}
import json, sys
from pathlib import Path
prompt = sys.argv[sys.argv.index("-p") + 1]
item = json.loads(prompt.split("The item:\\n\\n", 1)[1])
with open(Path(__file__).with_name("shown.jsonl"), "a", encoding="utf-8") as shown:
    shown.write(json.dumps(item) + "\\n")
guess = {{"item_id": item["item_id"], "arm_guess": "A", "confidence": 0.4, "evidence_span": None, "evidence_kind": "no_basis"}}
print(json.dumps({{"structured_output": guess}}))
"""


def test_the_arm_guess_audit_shows_its_lane_exactly_the_judge_view_of_each_original_item(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    case = _case("case-1", required=(Required("release-freeze", "The release is frozen."),))
    sessions = [_session(layout, "case-1", arm, repeat, AttemptResult(OK, answer="The release is frozen."))
                for arm in protocol.ARM_IDS for repeat in (1, 2)]  # fmt: skip
    item_set = build_items([case], sessions, seed=7, inputs_digest="inputs", tool_names=())
    write_items(layout, item_set)
    for item_id in item_set.arm_key:
        _write_verdict(layout, "codex", item_id, {"r1": "used"})
    guesser = tmp_path / "client" / "guesser"
    guesser.parent.mkdir()
    guesser.write_text(STAND_IN_GUESSER.format(python=sys.executable), encoding="utf-8")
    guesser.chmod(0o755)

    unblinded = unblind(layout, routes=["codex"], expected=item_set.arm_key)
    run_guesses(layout, unblinded, model="stand-in", effort=None, claude=str(guesser), launcher=())

    shown = [json.loads(line) for line in (guesser.parent / "shown.jsonl").read_text(encoding="utf-8").splitlines()]
    originals = [item for item in item_set.items if not item_set.arm_key[item["item_id"]]["rejudge_of"]]
    # No arm, session, verdict or re-judged copy: each original item's judge view, once.
    assert sorted(shown, key=lambda i: i["item_id"]) == sorted(originals, key=lambda i: i["item_id"])


def _judged_run_whose_answers_describe_the_map(tmp_path: Path) -> tuple[Layout, Unblinded]:
    layout = Layout(tmp_path / "run")
    case = _case("case-1", required=(Required("release-freeze", "The release is frozen."),))
    sessions = [_session(layout, "case-1", arm, 1, AttemptResult(OK, answer="I read the map. The release is frozen."))
                for arm in protocol.ARM_IDS]  # fmt: skip
    item_set = build_items([case], sessions, seed=7, inputs_digest="inputs", tool_names=())
    write_items(layout, item_set)
    for item_id in item_set.arm_key:
        _write_verdict(layout, "codex", item_id, {"r1": "used"})
    return layout, unblind(layout, routes=["codex"], expected=item_set.arm_key)


def _guess_every_arm_right_from(layout: Layout, unblinded: Unblinded, span: str) -> None:
    (layout.audit / "guesses").mkdir(parents=True, exist_ok=True)
    for item_id, entry in unblinded.arm_key.items():
        if not entry["rejudge_of"]:
            guess = {"item_id": item_id, "arm_guess": entry["arm"], "confidence": 0.9, "evidence_span": span,
                     "evidence_kind": "describes_tools_or_retrieval"}  # fmt: skip
            (layout.audit / "guesses" / f"{item_id}.json").write_text(json.dumps(guess), encoding="utf-8")


def test_scoring_refuses_a_cached_rejudge_verdict_once_its_flagged_span_changes(tmp_path: Path) -> None:
    layout, unblinded = _judged_run_whose_answers_describe_the_map(tmp_path)
    _guess_every_arm_right_from(layout, unblinded, "I read the map.")
    for item in write_rejudge_set(layout, unblinded, seed=7, inputs_digest="inputs"):
        folder = layout.audit / "verdicts" / "codex"
        folder.mkdir(parents=True, exist_ok=True)
        verdict = {"item_id": item["item_id"], "required": {"r1": "missing"}, "forbidden": {}, "contaminated": None, "span": None}
        (folder / f"{item['item_id']}.json").write_text(json.dumps(verdict), encoding="utf-8")
    assert load_audit(layout, unblinded, seed=7, inputs_digest="inputs") is not None

    # A shorter span leaves "." in the answer: the cached verdicts judged another view.
    _guess_every_arm_right_from(layout, unblinded, "I read the map")

    with pytest.raises(UnblindingError):
        load_audit(layout, unblinded, seed=7, inputs_digest="inputs")


def test_the_audit_draws_its_controls_even_when_every_item_is_flagged(tmp_path: Path) -> None:
    layout, unblinded = _judged_run_whose_answers_describe_the_map(tmp_path)
    _guess_every_arm_right_from(layout, unblinded, "I read the map.")

    _items, key = rejudge_set(layout, unblinded, load_guesses(layout, unblinded), seed=7, inputs_digest="inputs")

    # The controls are unchanged copies, so a run with every item flagged still has its noise baseline.
    roles = [entry["role"] for entry in key.values()]
    assert roles.count("flagged") == roles.count("control") == len(protocol.ARM_IDS)


def test_a_rule_whose_verdict_flips_without_the_flagged_spans_reads_as_inconclusive(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    cases = [_case(f"case-{n}", kind="ordinary", required=(Required("release-freeze", "The release is frozen."),))
             for n in range(6)]  # fmt: skip
    sessions, verdicts = {}, {}
    for case in cases:
        for arm in protocol.ARM_IDS:
            record = _session(layout, case.case_id, arm, 1, AttemptResult(OK, answer="The release is frozen."))
            sessions[record["session_id"]] = record
            verdicts[record["session_id"]] = {"required": {"r1": "used" if arm == "M" else "missing"}, "forbidden": {}, "contaminated": None}
    # The auditor named every M answer from its talk of the map; without that span the judge finds the fact missing.
    audit = {
        "guesses": {sid: {"arm_guess": r["arm"], "confidence": 0.9, "evidence_kind": "describes_tools_or_retrieval",
                          "evidence_span": "I read the map."} for sid, r in sessions.items()},
        "flagged": {sid: {**verdicts[sid], "required": {"r1": "missing"}} for sid, r in sessions.items() if r["arm"] == "M"},
        "controls": {},
    }  # fmt: skip

    scored = score_run(
        layout, cases=cases, sessions=sessions, verdicts=verdicts, arm_ids=list(protocol.ARM_IDS), repeats=1,
        split=protocol.SPLIT_HELDOUT, deltas={2: 0.1, 3: 0.1}, seed=0, audit=audit,
    )  # fmt: skip

    assert scored["rules"]["3"]["holds"]
    assert any("arm-guess audit" in reason for reason in scored["rules"]["3"]["inconclusive"])


def test_a_run_whose_arm_guess_audit_has_not_run_does_not_switch() -> None:
    decided = {str(n): {"evaluable": True, "holds": True, "inconclusive": []} for n in range(1, 6)}

    assert decide(decided, void=False, split=protocol.SPLIT_HELDOUT, audited=False).startswith("inconclusive")


def test_the_tie_break_needs_the_arm_key_and_runs_only_after_unblinding(tmp_path: Path) -> None:
    layout = Layout(tmp_path / "run")
    layout.judge.mkdir(parents=True)
    layout.unblinding.mkdir(parents=True)
    (layout.judge / "items.jsonl").write_text(
        "".join(json.dumps({"item_id": i}) + "\n" for i in ("item-m", "item-a")), encoding="utf-8"
    )
    first = {"required": {"r1": "used"}, "forbidden": {"f1": "absent"}, "contaminated": None}
    second = {"required": {"r1": "missing"}, "forbidden": {"f1": "asserted_as_current"}, "contaminated": None}
    record_first_route(layout, "codex")
    for route, verdict in (("codex", first), ("claude", second)):
        (layout.judge / "verdicts" / route).mkdir(parents=True)
        for item_id in ("item-m", "item-a"):
            (layout.judge / "verdicts" / route / f"{item_id}.json").write_text(json.dumps({**verdict, "item_id": item_id}))

    with pytest.raises(UnblindingError):
        unblind(layout, routes=["codex", "claude"], expected={})  # no arm key yet
    with pytest.raises(UnblindingError):
        tie_break({**first, "item_id": "item-m"}, {**second, "item_id": "item-m"}, unblinded={"item-m": {"arm": "M"}})

    key = {
        "item-m": {"session_id": "s-m", "case_id": "c", "arm": "M", "repeat": 1, "rejudge_of": None},
        "item-a": {"session_id": "s-a", "case_id": "c", "arm": "A", "repeat": 1, "rejudge_of": None},
    }
    (layout.unblinding / "arm-key.json").write_text(json.dumps(key), encoding="utf-8")
    (layout.judge / "verdicts" / "claude" / "item-a.json").unlink()
    with pytest.raises(UnblindingError):
        unblind(layout, routes=["codex", "claude"], expected=key)  # the second judge has not judged every item
    (layout.judge / "verdicts" / "claude" / "item-a.json").write_text(json.dumps({**second, "item_id": "item-a"}))
    unblinded = unblind(layout, routes=["codex", "claude"], expected=key)

    # Where the judges disagree, the verdict that weighs against the switch stands.
    for_m = tie_break({**first, "item_id": "item-m"}, {**second, "item_id": "item-m"}, unblinded=unblinded)
    for_a = tie_break({**first, "item_id": "item-a"}, {**second, "item_id": "item-a"}, unblinded=unblinded)
    assert (for_m["required"]["r1"], for_m["forbidden"]["f1"]) == ("missing", "asserted_as_current")
    assert (for_a["required"]["r1"], for_a["forbidden"]["f1"]) == ("used", "absent")
