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
from membench.utility.switch_eval.tap import (
    ACTIVATION_WARMING_REASON,
    BUDGET_REFUSAL,
    RECALL_WARMING_CODE,
    REQUIRED_CLEAR_COMPONENTS,
    ready_wait,
)
from membench.utility.switch_eval.tap import __file__ as TAP_SCRIPT

from exomem.runtime_resources import CPU_THREADS_ENV


def test_arm_filters_keep_published_read_tools_and_remove_only_activation_from_pull_only() -> None:
    from exomem.commands import product_commands_for

    commands = {command.name: command for command in product_commands_for("mcp")}
    published = {command.public_name for command in commands.values()}
    activation = commands["activate_context"].public_name
    search = commands["ask_memory"].public_name
    specs = arm_specs()

    assert set(specs["A"].tools) <= published
    assert specs["A"].tools == specs["M"].tools
    assert activation in specs["A"].tools
    assert search in specs["P"].tools
    assert set(specs["P"].tools) == set(specs["A"].tools) - {activation}
    assert protocol.ACTIVATION_TOOL == protocol.P_FILTERED_TOOL == activation


# A stand-in MCP server: answers initialize, tools/list and tools/call; with
# --crash-on-call exits mid-call, as a crashing server under test would, and
# with --exit-at-start exits before it reads a message, as a broken build would.
FAKE_SERVER = """
import json, sys, time
from pathlib import Path
if "--startup-marker" in sys.argv:
    marker = Path(sys.argv[sys.argv.index("--startup-marker") + 1])
    if "--write-vault" in sys.argv:
        Path(sys.argv[sys.argv.index("--write-vault") + 1]).write_text("startup sidecar")
    marker.write_text(json.dumps({"outcome": "complete"}))
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
        if "--crash-on-probe" in sys.argv:
            sys.exit(3)
        if "--never-ready" in sys.argv or ("--warming-once" in sys.argv and probes == 1):
            if "--warming-once" in sys.argv:
                time.sleep(6)
            result = {"structuredContent": {"result": {"success": False,
                      "error": {"code": "RETRIEVAL_INDEX_WARMING"}}}}
        elif "--warming-once" in sys.argv and probes == 2:
            result = {"structuredContent": {"result": {"hits": [],
                      "warming": {"components": ["embeddings"], "since_s": None}}}}
        else:
            result = {"structuredContent": {"result": {"hits": [],
                      "warming": {"components": ["graph", "pending_visibility"], "since_s": None}}}}
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
        "--ready-timeout", "30" if eventually_ready else "0.05",
        "--server-stderr", str(slot.runtime / "server.stderr"),
        "--", sys.executable, str(server), "--check-meta",
        "--warming-once" if eventually_ready else "--never-ready",
    ))
    # Warm-up exceeds the turn limit; five seconds allow the interpreter starts.
    executor = ClaudeSessionExecutor(Layout(tmp_path), {"P": spec}, "m", "v", {}, turn_timeout=5)
    result = executor(inputs, tmp_path / "attempt")

    tap_events = _log(tmp_path / "attempt" / "runtime" / "tap.jsonl")
    ready = next(e for e in tap_events if e["event"] == "ready_finished")
    if not eventually_ready:
        assert result.outcome == HARNESS_FAULT and result.kind == "recall_not_ready"
        assert ready["outcome"] == "timeout"
        assert not any(e["event"] == "call" for e in tap_events)
        with pytest.raises(RunAborted, match="recall_not_ready after 2 retries"):
            run_session(inputs, tmp_path / "session", lambda *args: result)
        return

    assert result.outcome == OK and result.answer == "served 1"
    assert ready["outcome"] == "ready" and ready["probes"] == 3
    assert next(e["t"] for e in tap_events if e["event"] == "tools_list") >= ready["t"]
    turn = result.turns[0]
    assert turn["calls"] == 1 and turn["calls_refused_budget"] == 0
    assert turn["ready_wait_seconds"] >= 8.0 and turn["wall_seconds"] < 5
    record = run_session(inputs, tmp_path / "session", lambda *args: result)
    assert record["facts"]["ready_gate"]["warming"] == {
        "components": ["graph", "pending_visibility"], "since_s": None,
    }


def test_a_lost_tap_cannot_suspend_the_turn_clock_past_its_gate_bound() -> None:
    events = [{"event": "ready_started", "monotonic": 100, "timeout_seconds": 5}]

    assert ready_wait(events, 100, 104) == 4
    assert ready_wait(events, 100, 112) == 5
    assert ready_wait(events, 107, 112) == 0


def test_a_server_exit_during_the_gate_is_a_retried_setup_fault(tmp_path: Path) -> None:
    tap, log, _turn_file = _start_tap(tmp_path, "--crash-on-probe")
    _ask(tap, 1, "initialize", {})
    assert _ask(tap, 2, "tools/list", {}) is None
    tap.wait(timeout=30)
    events = _log(log)

    def exited(inputs: SessionInputs, attempt_dir: Path) -> AttemptResult:
        return classify(
            spec=arm_specs()["P"], model="m", client_version="v", events=[], turn_records=[],
            tap_events=events, scored_turn=0, timed_out=False, quota=None, surface=None,
        )

    result = exited(_inputs(), tmp_path)
    assert result.outcome == HARNESS_FAULT and result.kind == "recall_not_ready"
    with pytest.raises(RunAborted, match="recall_not_ready after 2 retries"):
        run_session(_inputs(), tmp_path / "session", exited)
    assert len(_log(tmp_path / "session" / "attempts.jsonl")) == 3


def test_the_warming_guard_tracks_the_product_refusal_constants() -> None:
    from exomem.activation_result import INDEX_WARMING
    from exomem.find import _RECALL_PROJECTION_STALE_COMPONENT, RetrievalIndexWarming
    from exomem.readiness import COMPONENTS

    assert REQUIRED_CLEAR_COMPONENTS == {_RECALL_PROJECTION_STALE_COMPONENT, "embeddings", "clip"}
    assert {"embeddings", "clip"} <= set(COMPONENTS)
    assert (ACTIVATION_WARMING_REASON, RECALL_WARMING_CODE) == (
        INDEX_WARMING, RetrievalIndexWarming(site="catalog_proof_incomplete").code,
    )


def test_surface_capture_never_probes_or_waits_for_recall(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from membench.utility.switch_eval import state

    slot = Slot(tmp_path / "slot")
    slot.runtime.mkdir(parents=True)
    slot.cwd.mkdir()
    server = tmp_path / "stand_in_server.py"
    server.write_text(FAKE_SERVER, encoding="utf-8")
    real_entry = state.mcp_server_entry

    def stand_in_entry(*args, **kwargs):
        entry = real_entry(*args, **kwargs)
        # Only replace the product process; keep the surface stage's real tap configuration.
        entry["args"][entry["args"].index("--") + 1:] = [sys.executable, str(server), "--crash-on-probe"]
        return entry

    monkeypatch.setattr(state, "mcp_server_entry", stand_in_entry)
    spec = replace(arm_specs()["P"], tools=("read_memory",))
    out = tmp_path / "surface"
    surface = state.capture_surface(slot, spec, env={}, out=out)

    assert surface["per_tool"]
    events = [e for path in out.glob("*-tap.jsonl") for e in _log(path)]
    assert len([e for e in events if e["event"] == "tools_list"]) == 2
    assert not any(e["event"] in ("ready_started", "ready_finished", "call") for e in events)


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
            "wall_seconds": 1.0, "ready_wait_seconds": 2.0, "hook_responses": []}  # fmt: skip
    record = _session(
        layout, "case-1", "A", 1,
        AttemptResult(HARNESS_FAULT, "rate_limit", turns=[turn]),
        AttemptResult(HARNESS_FAULT, "transport", turns=[turn]),
        AttemptResult(OK, answer="An answer.", turns=[turn], facts={"ready_gate": {"outcome": "ready", "warming": {"components": ["embeddings", "graph"]}}}),
    )  # fmt: skip
    verdict = {"required": {}, "forbidden": {}, "contaminated": None}

    scored = score_run(
        layout, cases=[_case("case-1")], sessions={record["session_id"]: record}, verdicts={record["session_id"]: verdict},
        arm_ids=["A"], repeats=1, split=protocol.SPLIT_HELDOUT, deltas={2: 0.1, 3: 0.1}, seed=0,
        project_sessions=12,
    )  # fmt: skip

    assert scored["usage"]["totals"] == {"input_tokens": 300, "output_tokens": 30, "wall_seconds": 3.0, "ready_wait_seconds": 6.0, "attempts": 3}
    assert scored["warming_at_gate"]["A"] == {
        "sessions": 1, "unrecorded": 0, "not_ready": 0,
        "by_components": [{"components": ["embeddings", "graph"], "sessions": 1}],
    }
    assert scored["usage"]["projected"]["ready_wait_seconds"] == 72.0
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
    assert scored["warming_at_gate"]["A"]["unrecorded"] == 1
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

    assert scored["usage"]["totals"] == {"input_tokens": 100, "output_tokens": 10, "wall_seconds": 1.0, "ready_wait_seconds": None, "attempts": 1}


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


def test_the_arm_guess_audit_shows_its_lane_exactly_the_judge_view_of_each_original_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CPU_THREADS_ENV, "8")
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
    script = STAND_IN_GUESSER.format(python=sys.executable)
    script = script.replace("\n", f"\nimport os\nassert {CPU_THREADS_ENV!r} not in os.environ\n", 1)
    guesser.write_text(script, encoding="utf-8")
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


@pytest.fixture
def built_slot_tree(tmp_path: Path) -> Layout:
    """A built arm with nested text and binary files and disposable machine state."""
    from membench.utility.switch_eval.state import signature_digest, tree_digest

    layout = Layout(tmp_path / "run")
    arm = layout.arm_tree("P", "snapshot")
    shared = layout.shared_tree("snapshot")
    (shared / "vault" / "notes").mkdir(parents=True)
    (shared / "vault" / "notes" / "page.md").write_text("A frozen observation.")
    (shared / "vault" / "attachment.bin").write_bytes(bytes(range(256)))
    (shared / "vault" / "_schema.yaml").write_text("schema: 1")
    (arm / "state").mkdir(parents=True)
    (arm / "state" / "index.sqlite").write_bytes(b"machine state")
    (arm / "state.json").write_text(json.dumps({
        "vault_digest": tree_digest(shared / "vault"), "state_digest": tree_digest(arm / "state"),
        "build": {"vectors": {"state": "current"}, "serving_freshness": {"key_sha256": "fixture"}},
        "quiescence": {"content_unchanged": True, "signatures_unchanged": True},
        "vault_signature_digest": signature_digest(shared / "vault"),
    }))
    return layout


def test_restoring_and_recovering_a_slot_preserves_every_file_signature(built_slot_tree: Layout) -> None:
    from membench.utility.switch_eval.state import prepare_session_slot, recover_session_vault

    layout = built_slot_tree
    source = layout.shared_tree("snapshot") / "vault"
    before = {p.relative_to(source): (p.stat().st_mtime_ns, p.stat().st_ctime_ns, p.stat().st_size)
              for p in source.rglob("*") if p.is_file()}
    slot = prepare_session_slot(layout, "P", "snapshot", "first-session")
    assert not source.exists()
    assert {p.relative_to(slot.vault): (p.stat().st_mtime_ns, p.stat().st_ctime_ns, p.stat().st_size)
            for p in slot.vault.rglob("*") if p.is_file()} == before
    # A killed runner left this slot behind. The next preparation must recover it before wiping.
    slot = prepare_session_slot(layout, "P", "snapshot", "after-crash")
    recover_session_vault(layout)
    slot.wipe()
    assert {p.relative_to(source): (p.stat().st_mtime_ns, p.stat().st_ctime_ns, p.stat().st_size)
            for p in source.rglob("*") if p.is_file()} == before


def test_a_session_that_changes_the_vault_stops_the_run_and_preserves_it(built_slot_tree: Layout, tmp_path: Path) -> None:
    from membench.utility.switch_eval.state import StateError, prepare_session_slot

    layout = built_slot_tree
    changed = layout.slot / "vault" / "notes" / "page.md"
    inputs = replace(_inputs(arm="P"), cwd=layout.slot / "cwd", argv=(
        sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('changed')", str(changed),
    ))
    executor = ClaudeSessionExecutor(layout, {"P": arm_specs()["P"]}, "m", "v", {})
    with pytest.raises(RunAborted, match=f"{inputs.planned.session_id}.*vault digest changed"):
        run_session(inputs, tmp_path / "session", executor)
    assert changed.read_text() == "changed"
    assert not (layout.shared_tree("snapshot") / "vault").exists()
    # Recovery cannot silently bless the changed vault on a later run either.
    with pytest.raises(StateError, match="vault digest changed"):
        prepare_session_slot(layout, "P", "snapshot", "next-session")
    assert changed.read_text() == "changed"


@pytest.mark.parametrize("failure", [OSError("actor launch failed"), KeyboardInterrupt()])
def test_session_exceptions_return_the_unchanged_vault(built_slot_tree: Layout, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: BaseException) -> None:
    layout = built_slot_tree
    executor = ClaudeSessionExecutor(layout, {"P": arm_specs()["P"]}, "m", "v", {})

    def interrupted(*args):
        raise failure

    monkeypatch.setattr(executor, "_execute", interrupted)
    with pytest.raises(type(failure)):
        executor(_inputs(arm="P"), tmp_path / "attempt")
    assert (layout.shared_tree("snapshot") / "vault" / "notes" / "page.md").read_text() == "A frozen observation."
    assert not layout.slot.exists()
    assert not (layout.run / "slot-vault.json").exists()


def test_the_builder_publishes_vectors_and_the_active_pointer(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import socket

    import numpy as np
    from membench.utility.switch_eval import state
    from membench.utility.switch_eval.state import build_state

    from exomem import embedding_backend, embeddings, index_paths, recall_migration, state_paths

    # Keep the real product publication and storage; replace only the encoder boundary.
    class Encoder:
        device = "cpu"
        concurrent_encodes = False
        profile = embedding_backend.EncoderProfile(
            model="fixture/encoder", pooling="cls", query_prefix="", passage_prefix="",
            max_seq=512, pad_token="<pad>", revision="0" * 40, quantization="fixture",
            file_format="fixture", artifact_digest="fixture",
        )

        def encode(self, texts, **kwargs):
            return np.ones((len(texts), 8), dtype=np.float32) / np.sqrt(8)

        def release(self):
            pass

    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setenv("EXOMEM_DISABLE_MEDIA_EXTRACTION", "1")
    monkeypatch.setenv("EXOMEM_MODEL_OFFLINE", "1")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")

    def refuse_connection(*args, **kwargs):
        raise AssertionError("synthetic builder attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse_connection)
    monkeypatch.setattr(socket, "create_connection", refuse_connection)
    monkeypatch.delenv(recall_migration.REEMBED_ENV, raising=False)
    monkeypatch.setattr(embeddings, "MODEL_NAME", "fixture/encoder")
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    monkeypatch.setattr(embedding_backend, "_importable", lambda module: True)
    monkeypatch.setattr(embedding_backend, "load_encoder", lambda *args, **kwargs: Encoder())
    observed_source = {"source_revision": "fixture-builder", "origin": "source-tree"}
    monkeypatch.setattr(state, "product_source_identity", lambda: observed_source)
    try:
        built = build_state(vault)
        pointer = state_paths.vault_state_dir(vault) / index_paths.ACTIVE_SIDECAR_POINTER
        sidecar = index_paths.sidecar_path(vault)
        assert pointer.read_text().strip() == sidecar.name
        assert sidecar.is_file()
        assert embeddings.get_embedding_index(vault).file_mtimes()
        assert built["vectors"]["state"] == "current"
        assert built["vault_digest"] == state.tree_digest(vault)
        assert built["product_source"] == observed_source
        # Sessions serve cold copies: the sidecars must already carry the key the server asks for,
        # or every map and packet reads as stale.
        from exomem import context_catalogue, find, working_set_index, working_set_runtime

        serving_key = working_set_runtime._key_text(find.FreshnessSnapshot(vault).projection_key("kb"))
        reopened_index = working_set_index.WorkingSetIndex(vault)
        try:
            assert reopened_index.freshness_stamp() == serving_key
        finally:
            reopened_index.close()
        reopened_catalogue = context_catalogue.ContextCatalogue(vault)
        assert reopened_catalogue.freshness_stamp() == serving_key
        assert not reopened_catalogue.pending()

        page = next(path for path in index_paths.iter_index_markdown(vault)
                    if embeddings.reconstruct_publication(vault, path) is not None)
        info = page.stat()
        page.write_text(page.read_text() + "\nA changed observation with the same modification time.\n")
        os.utime(page, ns=(info.st_atime_ns, info.st_mtime_ns))
        assert embeddings.reconstruct_publication(vault, page) is None
        media_parent = page.parent / "extracted-attachment.md"

        def media_output(root):
            media_parent.write_text("---\ntype: note\n---\n# Extracted attachment\n\nFinal media content.\n")
            return {"passes": [{"attempted": 1, "files_written": 1}], "files_written": 1}

        monkeypatch.setattr(state, "converge_media", media_output)
        rebuilt = build_state(vault)
        assert embeddings.reconstruct_publication(vault, page) is not None
        assert embeddings.reconstruct_publication(vault, media_parent) is not None
        assert rebuilt["vectors"]["coverage"]["freshly_published_parents"] >= 1
        assert rebuilt["convergence"]["files_written"] == 1

        # A timed segmenter cannot reconstruct without inference. Its local write
        # can still finish both projections without returning a volatile proof.
        reconstruct = embeddings.reconstruct_publication
        upsert = embeddings.upsert_after_write_status
        monkeypatch.setattr(embeddings, "reconstruct_publication", lambda root, path: (
            None if path == media_parent else reconstruct(root, path)
        ))
        monkeypatch.setattr(state, "converge_media", lambda root: {"passes": [], "files_written": 0})
        fresh = build_state(vault)
        assert fresh["vectors"]["coverage"]["freshly_published_parents"] == 1

        def ineligible(root, paths, **kwargs):
            return replace(upsert(root, paths, **kwargs), eligible_count=0)

        monkeypatch.setattr(embeddings, "upsert_after_write_status", ineligible)
        with pytest.raises(state.StateError, match="did not complete"):
            build_state(vault)

        def source_changed(root, paths, **kwargs):
            status = upsert(root, paths, **kwargs)
            info = paths[0].stat()
            os.utime(paths[0], ns=(info.st_atime_ns, info.st_mtime_ns + 1))
            return status

        monkeypatch.setattr(embeddings, "upsert_after_write_status", source_changed)
        with pytest.raises(state.StateError, match="source changed"):
            build_state(vault)

        from exomem.context_catalogue import ContextCatalogue

        monkeypatch.setattr(embeddings, "upsert_after_write_status", upsert)
        monkeypatch.setattr(embeddings, "reconstruct_publication", reconstruct)
        owner_coverage = ContextCatalogue.owner_coverage

        def changed_during_catalogue_report(catalogue):
            result = owner_coverage(catalogue)
            page.write_text(page.read_text() + "\nA write after vector coverage.\n")
            return result

        monkeypatch.setattr(ContextCatalogue, "owner_coverage", changed_during_catalogue_report)
        with pytest.raises(state.StateError, match="changed during final coverage"):
            build_state(vault)
    finally:
        embeddings.unload_model()
        embeddings.clear_embedding_indexes()
        recall_migration.reset_for_tests()


def test_a_same_bytes_rewrite_cannot_return_as_pristine(built_slot_tree: Layout) -> None:
    import os

    from membench.utility.switch_eval.state import (
        StateError,
        prepare_session_slot,
        recover_session_vault,
    )

    layout = built_slot_tree
    slot = prepare_session_slot(layout, "P", "snapshot", "rewrite-session")
    page = slot.vault / "notes" / "page.md"
    content = page.read_bytes()
    # An atomic same-content rewrite changes ctime even when mtime is restored.
    replacement = page.with_suffix(".new")
    replacement.write_bytes(content)
    info = page.stat()
    os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
    replacement.replace(page)
    with pytest.raises(StateError, match="rewrite-session.*signatures.*rebuild state"):
        recover_session_vault(layout)
    assert page.read_bytes() == content
    assert (layout.run / "slot-vault.json").exists()
    assert not (layout.shared_tree("snapshot") / "vault").exists()


def test_a_startup_write_refuses_the_build_instead_of_blessing_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from membench.utility.switch_eval import state

    layout = Layout(tmp_path / "run")
    source = tmp_path / "source"
    source.mkdir()
    (source / "page.md").write_text("Frozen content.")
    server = tmp_path / "stand_in_server.py"
    server.write_text(FAKE_SERVER)
    spec = replace(arm_specs()["P"], tools=("read_memory",))
    real_entry = state.mcp_server_entry

    def stand_in_entry(*args, **kwargs):
        entry = real_entry(*args, **kwargs)
        child = [sys.executable, str(server)]
        if kwargs.get("startup_marker") is not None:
            child += ["--startup-marker", str(kwargs["startup_marker"]), "--write-vault", str(layout.slot / "vault" / "attachment.txt.md")]
        entry["args"][entry["args"].index("--") + 1:] = child
        return entry

    def built(*args, **kwargs):
        (layout.slot / "state").mkdir()
        (layout.slot / "state" / "index.sqlite").write_bytes(b"built state")
        return subprocess.CompletedProcess(args[0], 0, json.dumps({
            "vectors": {"state": "current"}, "serving_freshness": {"key_sha256": "fixture"}, "vault_digest": state.tree_digest(layout.slot / "vault"),
        }), "")

    monkeypatch.setattr(state, "mcp_server_entry", stand_in_entry)
    monkeypatch.setattr(state.subprocess, "run", built)
    with pytest.raises(state.StateError, match="fresh server changed the vault.*'.md': 1"):
        state.build_arm(layout, spec, tree="snapshot", source_vault=source, parent_env={})
    proof = json.loads((layout.shared_tree("snapshot") / "quiescence.json").read_text())
    assert proof["content_unchanged"] is False
    assert proof["changed_files_by_extension"] == {".md": 1}
    assert (layout.slot / "vault" / "attachment.txt.md").read_text() == "startup sidecar"
    assert not (layout.shared_tree("snapshot") / "state.json").exists()


def test_an_interrupted_build_retries_without_nesting_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from membench.utility.switch_eval import state

    layout = Layout(tmp_path / "run")
    source = tmp_path / "source"
    source.mkdir()
    (source / "page.md").write_text("Frozen content.")
    spec = arm_specs()["P"]
    signatures = []

    def built(*args, **kwargs):
        (layout.slot / "state").mkdir()
        (layout.slot / "state" / "index.sqlite").write_bytes(b"built state")
        return subprocess.CompletedProcess(args[0], 0, json.dumps({
            "vectors": {"state": "current"}, "serving_freshness": {"key_sha256": "fixture"}, "vault_digest": state.tree_digest(layout.slot / "vault"),
        }), "")

    def proof(slot, *args, **kwargs):
        signatures.append(state.signature_digest(slot.vault))
        if len(signatures) == 1:
            raise RuntimeError("interrupted proof")
        return {"content_unchanged": True, "signatures_unchanged": True}

    monkeypatch.setattr(state.subprocess, "run", built)
    monkeypatch.setattr(state, "capture_surface", lambda *args, **kwargs: {})
    monkeypatch.setattr(state, "prove_quiescence", proof)
    with pytest.raises(RuntimeError, match="interrupted proof"):
        state.build_arm(layout, spec, tree="snapshot", source_vault=source, parent_env={})
    record = state.build_arm(layout, spec, tree="snapshot", source_vault=source, parent_env={})
    assert record["resumed"] is True
    assert signatures == [record["vault_signature_digest"]] * 2
    assert (layout.shared_tree("snapshot") / "state" / "index.sqlite").is_file()
    assert not (layout.shared_tree("snapshot") / "state" / "state").exists()
    assert (layout.shared_tree("snapshot") / "vault" / "page.md").read_text() == "Frozen content."


def test_the_real_startup_wrapper_refuses_unconverged_media(tmp_path: Path) -> None:
    """Catch writes during real startup; this canary does not exercise the media-queue join."""
    import os
    import shutil

    from membench.utility.switch_eval import state

    REPO_ROOT = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    shutil.copytree(REPO_ROOT / "src" / "exomem" / "_scaffold" / "_Schema", source / "Knowledge Base" / "_Schema")
    attachment = source / "Knowledge Base" / "attachment.txt"
    attachment.write_text("A synthetic text attachment.")
    layout = Layout(tmp_path / "run")
    slot = state.prepare_build_slot(layout, source)
    spec = replace(arm_specs()["P"], profile="neutral-lexical")
    before = {"vault_digest": state.tree_digest(slot.vault), "vault_signatures": state.signature_manifest(slot.vault)}
    out = tmp_path / "proof"
    out.mkdir()
    with pytest.raises(state.StateError, match="fresh server changed the vault"):
        state.prove_quiescence(slot, spec, env=state._child_env(os.environ, slot, spec), out=out, before=before)
    assert (slot.vault / "Knowledge Base" / "attachment.txt.md").is_file()
    assert json.loads((out / "quiescence.json").read_text())["content_unchanged"] is False


def _governed_vault_source(root: Path) -> Path:
    """A scaffold vault with one page and a governance rule, so every recall is audited."""
    import shutil

    REPO_ROOT = Path(__file__).resolve().parents[1]
    source = root / "source"
    shutil.copytree(REPO_ROOT / "src" / "exomem" / "_scaffold" / "_Schema", source / "Knowledge Base" / "_Schema")
    (source / "Knowledge Base" / "readiness.md").write_text(
        "# Readiness\n\nA readiness probe note that any recall for readiness finds.\n"
    )
    governance = source / "Knowledge Base" / "_Governance"
    (governance / "scopes").mkdir(parents=True)
    (governance / "rules").mkdir()
    (governance / "scopes" / "planning.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5F01\nname: Planning\npaths: [\"Planning/**\"]\n"
    )
    (governance / "rules" / "external.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5F02\n"
        "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5F01\"]\naudience: external\nceiling: 6\n"
    )
    return source


def test_the_real_startup_proof_files_its_readiness_receipts_out_of_a_governed_vault(tmp_path: Path) -> None:
    """Under a governance policy every recall appends a disclosure receipt to the vault.

    The tap's readiness probe is such a recall. The proof files the receipt
    instance into run evidence, then demands the vault it started with.
    """
    import os

    from membench.utility.switch_eval import state

    source = _governed_vault_source(tmp_path)
    layout = Layout(tmp_path / "run")
    slot = state.prepare_build_slot(layout, source)
    spec = replace(arm_specs()["P"], profile="neutral-lexical")
    before = {"vault_digest": state.tree_digest(slot.vault), "vault_signatures": state.signature_manifest(slot.vault)}
    out = tmp_path / "proof"
    out.mkdir()

    report = state.prove_quiescence(slot, spec, env=state._child_env(os.environ, slot, spec), out=out, before=before)

    events = [json.loads(line) for line in (out / "surface-server-discover-tap.jsonl").read_text().splitlines()]
    assert [event["outcome"] for event in events if event["event"] == "ready_finished"] == ["ready"]
    assert report["receipts_filed"]["by_event_type"].get("disclosure", 0) >= 1, report
    assert report["content_unchanged"] and report["signatures_unchanged"], report
    events_path = Path("Knowledge Base") / "_Governance" / "events"
    assert any((out / "receipts" / events_path).iterdir())
    assert not any((slot.vault / events_path).iterdir())


def test_a_governed_session_read_is_filed_and_the_next_session_starts(tmp_path: Path) -> None:
    """The first real session reads through the tap, which appends a receipt to the vault.

    Without filing, recovery refuses the changed vault and stops the run. A real
    snapshot already holds receipt instances from earlier reads: they stay.
    """
    import os
    import shutil

    from membench.utility.switch_eval import state

    from exomem.governance import receipts

    layout = Layout(tmp_path / "run")
    spec = replace(arm_specs()["P"], profile="neutral-lexical")

    def environment(slot):
        return state._child_env(os.environ, slot, spec)

    events_path = Path("Knowledge Base") / "_Governance" / "events"
    reads = tmp_path / "reads"
    (reads / "earlier").mkdir(parents=True)
    (reads / "session").mkdir()
    # A real governed read before checkout leaves one receipt instance in the snapshot.
    earlier = state.prepare_build_slot(layout, _governed_vault_source(tmp_path))
    state._ask_surface(earlier, spec, env=environment(earlier), out=reads / "earlier", method="server/discover", startup_proof=True)
    shared = layout.shared_tree("snapshot") / "vault"
    shared.parent.mkdir(parents=True)
    shutil.move(str(earlier.vault), str(shared))
    earlier.wipe()
    existing = sorted(entry.name for entry in (shared / events_path).iterdir())
    assert len(existing) == 1
    machine_state = layout.arm_tree("P", "snapshot") / "state"
    machine_state.mkdir(parents=True)
    baseline = {"digest": state.tree_digest(shared), "signatures": state.signature_manifest(shared)}
    (layout.arm_tree("P", "snapshot") / "state.json").write_text(json.dumps({
        "vault_digest": baseline["digest"], "state_digest": state.tree_digest(machine_state),
        "build": {"vectors": {"state": "current"}, "serving_freshness": {"key_sha256": "fixture"}},
        "quiescence": {"content_unchanged": True, "signatures_unchanged": True},
        "vault_signature_digest": state.signature_digest(shared),
    }))

    slot = state.prepare_session_slot(layout, "P", "snapshot", "governed-session")
    state._ask_surface(slot, spec, env=environment(slot), out=reads / "session", method="server/discover", startup_proof=True)
    assert len(list((slot.vault / events_path).iterdir())) == 2  # the served read left a receipt instance
    recovery = state.recover_session_custody(layout)

    assert recovery.digest == baseline["digest"]
    assert state.signature_manifest(shared) == baseline["signatures"]
    assert sorted(entry.name for entry in (shared / events_path).iterdir()) == existing
    assert recovery.receipts_filed["instances"] == 1
    assert recovery.receipts_filed["by_event_type"].get("disclosure", 0) >= 1, recovery
    evidence = layout.receipts_dir("governed-session")
    assert not set(existing) & {entry.name for entry in (evidence / events_path).iterdir()}
    assert {record["event_type"] for record in receipts.event_records(evidence)} >= {"disclosure"}
    state.prepare_session_slot(layout, "P", "snapshot", "next-session")


def test_a_filed_receipt_chain_that_fails_verification_names_the_vault_and_the_run(built_slot_tree: Layout) -> None:
    from membench.utility.switch_eval.state import (
        StateError,
        prepare_session_slot,
        recover_session_vault,
    )

    layout = built_slot_tree
    slot = prepare_session_slot(layout, "P", "snapshot", "broken-chain")
    instance = slot.vault / "Knowledge Base" / "_Governance" / "events" / ("b" * 32)
    instance.mkdir(parents=True)
    (instance / "2026-10.jsonl").write_text("not a receipt\n")

    with pytest.raises(StateError, match="chain verification") as refused:
        recover_session_vault(layout)

    message = str(refused.value)
    assert all(str(place) in message for place in (slot.vault, layout.receipts_dir("broken-chain"), layout.run))
    assert (layout.run / "slot-vault.json").exists()


def test_a_receipts_directory_that_leaves_the_vault_is_refused_not_followed(built_slot_tree: Layout, tmp_path: Path) -> None:
    from membench.utility.switch_eval.state import (
        StateError,
        prepare_session_slot,
        recover_session_vault,
    )

    layout = built_slot_tree
    slot = prepare_session_slot(layout, "P", "snapshot", "linked-events")
    outside = tmp_path / "outside"
    (outside / ("c" * 32)).mkdir(parents=True)
    events = slot.vault / "Knowledge Base" / "_Governance" / "events"
    events.parent.mkdir(parents=True)
    events.symlink_to(outside)

    with pytest.raises(StateError, match="escaped the vault"):
        recover_session_vault(layout)

    assert (outside / ("c" * 32)).is_dir()


def test_a_kb_folder_override_is_refused_unless_it_names_the_servers_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from membench.utility.switch_eval.state import StateError, require_default_kb_dirname

    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Knowledge Base")
    require_default_kb_dirname()
    monkeypatch.setenv("EXOMEM_KB_DIRNAME", "Notes")
    with pytest.raises(StateError, match="EXOMEM_KB_DIRNAME"):
        require_default_kb_dirname()


def test_state_cpu_threads_reach_only_the_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from membench.utility.switch_eval import __main__ as cli
    from membench.utility.switch_eval import state
    from membench.utility.switch_eval.session import session_env

    layout = Layout(tmp_path / "run")
    source = tmp_path / "source"
    source.mkdir()
    (source / "page.md").write_text("Frozen content.")
    spec = arm_specs()["P"]
    inputs = {"arms": ["P"], "trees": {"snapshot": {"path": str(source)}}}
    monkeypatch.setattr(
        cli,
        "_context",
        lambda path: (layout, inputs, [SimpleNamespace(tree="snapshot")], [], {"P": spec}),
    )
    monkeypatch.setattr(cli, "_stage", lambda *args: None)
    monkeypatch.setattr(state, "effective_online_cpus", lambda: 2)
    monkeypatch.setenv(CPU_THREADS_ENV, "99")
    # The product promotes this legacy name to the setting above at import.
    monkeypatch.setenv("KB_MCP_CPU_THREADS", "7")
    real_run = subprocess.run
    observed = []

    def built(*args, **kwargs):
        (layout.slot / "state").mkdir()
        (layout.slot / "state" / "index.sqlite").write_bytes(b"built state")
        code = f"import json, os; print(json.dumps({{'vectors': {{'state': 'current'}}, 'serving_freshness': {{'key_sha256': 'fixture'}}, 'vault_digest': {state.tree_digest(layout.slot / 'vault')!r}, 'cpu_threads': os.environ.get({CPU_THREADS_ENV!r})}}))"
        result = real_run(
            [sys.executable, "-c", code],
            env=kwargs["env"],
            cwd=kwargs["cwd"],
            capture_output=True,
            text=True,
            check=True,
        )
        observed.append(json.loads(result.stdout)["cpu_threads"])
        return result

    def surface(slot, spec, *, env, out):
        assert CPU_THREADS_ENV not in env
        assert "KB_MCP_CPU_THREADS" not in env
        return {}

    def proof(slot, spec, *, env, out, before):
        assert CPU_THREADS_ENV not in env
        assert "KB_MCP_CPU_THREADS" not in env
        return {"content_unchanged": True, "signatures_unchanged": True}

    monkeypatch.setattr(state.subprocess, "run", built)
    monkeypatch.setattr(state, "capture_surface", surface)
    monkeypatch.setattr(state, "prove_quiescence", proof)
    assert cli.main(["state", "--run", str(layout.run), "--cpu-threads", "2"]) == 0
    assert observed == ["2"]
    record = json.loads((layout.shared_tree("snapshot") / "state.json").read_text())
    assert record["cpu_threads"] == 2
    assert record["resumed"] is False
    frozen_hashes = json.loads((layout.run / "arm-hashes.json").read_text())
    arm_record_path = layout.arm_tree("P", "snapshot") / "state.json"
    arm_record = json.loads(arm_record_path.read_text())
    arm_record["cpu_threads"] = 1
    arm_record_path.write_text(json.dumps(arm_record))
    assert cli._arm_hashes(layout, inputs, {"P": spec}, ["snapshot"]) == frozen_hashes
    slot = state.prepare_session_slot(layout, "P", "snapshot", "cpu-isolation")
    result = real_run(
        [sys.executable, "-c", f"import os; print(os.environ.get({CPU_THREADS_ENV!r}, 'unset'))"],
        env=session_env({CPU_THREADS_ENV: "99"}, slot, spec),
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "unset"
    state.recover_session_vault(layout)
    slot.wipe()
    for invalid in (0, 3):
        with pytest.raises(SystemExit, match="--cpu-threads must be between 1 and 2"):
            cli.main(["state", "--run", str(layout.run), "--cpu-threads", str(invalid)])


def test_an_interrupted_arm_copy_retries_from_the_shared_baseline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from membench.utility.switch_eval import state

    layout = Layout(tmp_path / "run")
    source = tmp_path / "source"
    source.mkdir()
    (source / "page.md").write_text("Frozen content.")
    builds = []
    captures = []

    def built(*args, **kwargs):
        builds.append(1)
        (layout.slot / "state").mkdir()
        (layout.slot / "state" / "index.sqlite").write_bytes(b"built state")
        return subprocess.CompletedProcess(
            args[0], 0, json.dumps({
                "vectors": {"state": "current"}, "serving_freshness": {"key_sha256": "fixture"}, "vault_digest": state.tree_digest(layout.slot / "vault"),
            }), ""
        )

    def surface(slot, spec, **kwargs):
        captures.append(spec.arm_id)
        if captures == ["P", "M"]:
            raise RuntimeError("interrupted arm capture")
        return {}

    monkeypatch.setattr(state.subprocess, "run", built)
    monkeypatch.setattr(state, "capture_surface", surface)
    monkeypatch.setattr(
        state,
        "prove_quiescence",
        lambda *args, **kwargs: {"content_unchanged": True, "signatures_unchanged": True},
    )
    state.build_arm(layout, arm_specs()["P"], tree="snapshot", source_vault=source, parent_env={})
    with pytest.raises(RuntimeError, match="interrupted arm capture"):
        state.build_arm(
            layout, arm_specs()["M"], tree="snapshot", source_vault=source, parent_env={}
        )
    record = state.build_arm(
        layout, arm_specs()["M"], tree="snapshot", source_vault=source, parent_env={}
    )
    assert record["arm"] == "M"
    assert builds == [1]
    assert captures == ["P", "M", "M"]
    assert (
        layout.arm_tree("M", "snapshot") / "state" / "index.sqlite"
    ).read_bytes() == b"built state"
    assert not (layout.arm_tree("M", "snapshot") / "state" / "state").exists()


@pytest.fixture
def seed_build_round(tmp_path: Path) -> tuple[Layout, Layout, Path]:
    from membench.utility.switch_eval import state

    from exomem import state_migration, state_paths

    layout, donor = Layout(tmp_path / "run"), Layout(tmp_path / "archive")
    source = tmp_path / "snapshot"
    source.mkdir()
    (source / "page.md").write_text("Frozen content.")
    baseline = donor.shared_tree("snapshot")
    (baseline / "vault").mkdir(parents=True)
    (baseline / "vault" / "page.md").write_text("Frozen content.")
    state_dir = baseline / "state" / state_paths.vault_state_key(layout.slot / "vault")
    state_dir.mkdir(parents=True)
    (state_dir / "vectors.bin").write_bytes(b"expensive compatible vectors")
    (state_dir / "lexical.sqlite").write_bytes(b"previous lexical schema")
    descriptors = state_migration.declared_descriptor_ids()
    (state_dir / state_migration.MANIFEST_NAME).write_text(json.dumps({
        "version": state_migration.MANIFEST_VERSION,
        "vault_identity": state_paths.vault_state_key(layout.slot / "vault"),
        "state": "complete", "descriptors": list(descriptors),
        "families": {name: {"status": "complete"} for name in descriptors},
    }))
    record = {
        "tree": "snapshot", "profile": state.profile_settings(arm_specs()["P"].profile),
        "snapshot_digest": state.tree_digest(source),
        "state_digest": state.tree_digest(baseline / "state"),
        "vault_digest": state.tree_digest(baseline / "vault"),
        "vault_signature_digest": state.signature_digest(baseline / "vault"),
        "build": {"vectors": {"state": "current"}, "serving_freshness": {"key_sha256": "fixture"}},
        "quiescence": {"content_unchanged": True, "signatures_unchanged": True},
        "cpu_threads": 6, "resumed": True,
        "vector_build": {"run": str(tmp_path / "first-round"), "cpu_threads": 6, "resumed": True},
        "preparation": {"cpu_threads": 2, "resumed": False},
    }
    (baseline / "state.json").write_text(json.dumps(record))
    return layout, donor, source


def test_seeded_state_republishes_and_captures_each_arm_with_original_vector_provenance(
    seed_build_round: tuple[Layout, Layout, Path], monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    from types import SimpleNamespace

    from membench.utility.switch_eval import __main__ as cli
    from membench.utility.switch_eval import state

    from exomem import state_paths

    layout, donor, source = seed_build_round
    baseline = donor.shared_tree("snapshot")
    metadata = (baseline / "state.json").read_bytes()
    original = json.loads(metadata)
    captures, builds = [], []
    specs = arm_specs()
    inputs = {"arms": ["P", "M"], "trees": {"snapshot": {"path": str(source)}}}
    monkeypatch.setattr(cli, "_context", lambda path: (
        layout, inputs, [SimpleNamespace(tree="snapshot")], [], specs,
    ))
    monkeypatch.setattr(cli, "_stage", lambda *args: None)
    monkeypatch.setattr(state, "effective_online_cpus", lambda: 2)

    def built(*args, **kwargs):
        state_dir = layout.slot / "state" / state_paths.vault_state_key(layout.slot / "vault")
        assert (state_dir / "vectors.bin").read_bytes() == b"expensive compatible vectors"
        assert (state_dir / "lexical.sqlite").read_bytes() == b"previous lexical schema"
        (state_dir / "lexical.sqlite").write_bytes(b"current lexical schema")
        builds.append(kwargs["env"][CPU_THREADS_ENV])
        return subprocess.CompletedProcess(args[0], 0, json.dumps({
            "vectors": {"state": "current", "chunks_processed": 0}, "serving_freshness": {"key_sha256": "fixture"},
            "vault_digest": state.tree_digest(layout.slot / "vault"),
            "owner_coverage": {"available": False, "reason": "catalogue_unavailable"},
        }), "")

    def surface(slot, spec, **kwargs):
        captures.append(spec.arm_id)
        return {"fresh": spec.arm_id}

    monkeypatch.setattr(state.subprocess, "run", built)
    monkeypatch.setattr(state, "capture_surface", surface)
    monkeypatch.setattr(state, "prove_quiescence", lambda *args, **kwargs: {
        "content_unchanged": True, "signatures_unchanged": True,
    })
    assert cli.main([
        "state", "--run", str(layout.run), "--cpu-threads", "1",
        "--seed-build-from", str(donor.run),
    ]) == 0
    record = json.loads((layout.shared_tree("snapshot") / "state.json").read_text())
    assert builds == ["1"]
    assert captures == ["P", "M"]
    assert record["vector_build"] == original["vector_build"]
    assert (record["cpu_threads"], record["resumed"]) == (6, True)
    assert record["preparation"] == {"cpu_threads": 1, "resumed": False}
    assert record["seed_build"]["donor_run"] == str(donor.run)
    assert (baseline / "state.json").read_bytes() == metadata
    assert state.tree_digest(baseline / "state") == original["state_digest"]
    assert not (baseline / "vault").exists()
    assert not (layout.shared_tree("snapshot") / "seed-build.json").exists()
    assert state.signature_digest(layout.shared_tree("snapshot") / "vault") == original["vault_signature_digest"]
    assert "catalogue_unavailable" in capsys.readouterr().out


@pytest.mark.parametrize("legacy_settings, expected_settings", [
    ({}, ("unrecorded", "unrecorded")),
    ({"cpu_threads": None, "resumed": False}, (None, False)),
])
def test_seed_and_score_preserve_unknown_legacy_settings_separately_from_explicit_defaults(
    seed_build_round: tuple[Layout, Layout, Path], monkeypatch: pytest.MonkeyPatch,
    legacy_settings: dict, expected_settings: tuple,
) -> None:
    from membench.utility.switch_eval import __main__ as cli
    from membench.utility.switch_eval import audit, judge, state

    layout, donor, source = seed_build_round
    metadata = donor.shared_tree("snapshot") / "state.json"
    original = json.loads(metadata.read_text())
    del original["cpu_threads"], original["resumed"], original["vector_build"], original["preparation"]
    original.update(legacy_settings)
    metadata.write_text(json.dumps(original))
    case = _case("legacy-settings")
    inputs = {
        "arms": ["P"], "trees": {"snapshot": {"path": str(source)}},
        "repeats": 1, "seed": 0, "harness_digest": "fixture-harness",
    }
    monkeypatch.setattr(cli, "_context", lambda path: (layout, inputs, [case], [], arm_specs()))
    monkeypatch.setattr(cli, "_stage", lambda *args: None)
    monkeypatch.setattr(state, "effective_online_cpus", lambda: 2)
    monkeypatch.setattr(state.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, json.dumps({
            "vectors": {"state": "current"}, "serving_freshness": {"key_sha256": "fixture"}, "vault_digest": state.tree_digest(layout.slot / "vault"),
        }), "",
    ))
    monkeypatch.setattr(state, "capture_surface", lambda *args, **kwargs: {})
    monkeypatch.setattr(state, "prove_quiescence", lambda *args, **kwargs: {
        "content_unchanged": True, "signatures_unchanged": True,
    })
    assert cli.main([
        "state", "--run", str(layout.run), "--cpu-threads", "1",
        "--seed-build-from", str(donor.run),
    ]) == 0
    record = json.loads((layout.shared_tree("snapshot") / "state.json").read_text())
    assert (record.get("cpu_threads", "unrecorded"), record.get("resumed", "unrecorded")) == expected_settings
    origin = record["vector_build"]
    assert (origin.get("cpu_threads", "unrecorded"), origin.get("resumed", "unrecorded")) == expected_settings
    assert record["preparation"] == {"cpu_threads": 1, "resumed": False}

    session = _session(layout, case.case_id, "P", 1, AttemptResult(ACTOR_FAILURE, "timeout"))
    monkeypatch.setattr(cli, "_standing", lambda *args: (
        {session["session_id"]: session}, None, {"seed": 0, "inputs_digest": "fixture-inputs"},
    ))
    monkeypatch.setattr(judge, "resolved_verdicts", lambda *args: {})
    monkeypatch.setattr(audit, "load_audit", lambda *args, **kwargs: None)
    assert cli.main(["score", "--run", str(layout.run)]) == 0
    scored = json.loads((layout.score / "score.json").read_text())["state_builds"]["snapshot"]
    assert (scored.get("cpu_threads", "unrecorded"), scored.get("resumed", "unrecorded")) == expected_settings
    assert scored["vector_build"] == origin
    assert scored["preparation"] == record["preparation"]


@pytest.mark.parametrize("mismatch", ["identity", "profile", "state", "snapshot", "manifest", "custody"])
def test_seed_mismatches_refuse_before_moving_the_original_vault(
    seed_build_round: tuple[Layout, Layout, Path], monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    from membench.utility.switch_eval import state

    from exomem import state_migration

    layout, donor, source = seed_build_round
    baseline = donor.shared_tree("snapshot")
    metadata = baseline / "state.json"
    record = json.loads(metadata.read_text())
    manifest = next((baseline / "state").rglob(state_migration.MANIFEST_NAME))
    if mismatch == "custody":
        (donor.run / "slot-vault.json").write_text("{}")
    elif mismatch == "identity":
        payload = json.loads(manifest.read_text())
        payload["vault_identity"] = "different-serving-path"
        manifest.write_text(json.dumps(payload))
        record["state_digest"] = state.tree_digest(baseline / "state")
    elif mismatch == "manifest":
        manifest.unlink()
        record["state_digest"] = state.tree_digest(baseline / "state")
    elif mismatch == "state":
        (manifest.parent / "vectors.bin").write_bytes(b"changed vectors")
    else:
        record[mismatch if mismatch == "profile" else "snapshot_digest"] = "changed"
    metadata.write_text(json.dumps(record))
    monkeypatch.setattr(state, "_rename_vault", lambda *args: pytest.fail("vault moved before validation"))
    with pytest.raises(state.StateError):
        state.build_arm(layout, arm_specs()["P"], tree="snapshot", source_vault=source,
                        parent_env={}, seed_build_from=donor.run)
    assert (baseline / "vault" / "page.md").read_text() == "Frozen content."
    assert not layout.slot.exists()
    assert not (layout.shared_tree("snapshot") / "seed-build.json").exists()


@pytest.mark.parametrize("interruption", ["rename", "builder", "surface", "proof"])
def test_seed_interruption_returns_custody_and_retries_from_the_retained_donor(
    seed_build_round: tuple[Layout, Layout, Path], monkeypatch: pytest.MonkeyPatch,
    interruption: str,
) -> None:
    from membench.utility.switch_eval import state

    layout, donor, source = seed_build_round
    baseline = donor.shared_tree("snapshot")
    original_state = state.tree_digest(baseline / "state")
    interrupted = False
    rename = state._rename_vault

    def interrupt(stage):
        nonlocal interrupted
        if stage == interruption and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()

    def move(*args):
        interrupt("rename")
        return rename(*args)

    def built(*args, **kwargs):
        assert state.tree_digest(layout.slot / "state") == original_state
        (layout.slot / "state" / "preparation-write").write_text("discard before retry")
        interrupt("builder")
        return subprocess.CompletedProcess(args[0], 0, json.dumps({
            "vectors": {"state": "current"}, "serving_freshness": {"key_sha256": "fixture"}, "vault_digest": state.tree_digest(layout.slot / "vault"),
        }), "")

    def surface(*args, **kwargs):
        interrupt("surface")
        return {}

    def proof(*args, **kwargs):
        interrupt("proof")
        return {"content_unchanged": True, "signatures_unchanged": True}

    monkeypatch.setattr(state, "_rename_vault", move)
    monkeypatch.setattr(state.subprocess, "run", built)
    monkeypatch.setattr(state, "capture_surface", surface)
    monkeypatch.setattr(state, "prove_quiescence", proof)
    with pytest.raises(KeyboardInterrupt):
        state.build_arm(layout, arm_specs()["P"], tree="snapshot", source_vault=source,
                        parent_env={}, seed_build_from=donor.run)
    target = layout.shared_tree("snapshot")
    assert (target / "seed-build.json").is_file()
    assert not (target / "state.json").exists()
    assert not (layout.run / "slot-vault.json").exists()
    assert not (layout.slot / "vault").exists()
    assert state.tree_digest(baseline / "state") == original_state
    with pytest.raises(state.StateError, match="--seed-build-from"):
        state.build_arm(layout, arm_specs()["P"], tree="snapshot", source_vault=source, parent_env={})
    state.build_arm(layout, arm_specs()["P"], tree="snapshot", source_vault=source,
                    parent_env={}, seed_build_from=donor.run)
    assert (target / "vault" / "page.md").read_text() == "Frozen content."
    assert not (target / "state" / "state").exists()
    assert state.tree_digest(baseline / "state") == original_state


def test_seed_rejects_an_archive_inside_the_slot_before_cleanup_can_delete_it(
    seed_build_round: tuple[Layout, Layout, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from membench.utility.switch_eval import state

    layout, donor, source = seed_build_round
    nested = layout.slot / "archive"
    nested.parent.mkdir(parents=True)
    donor.run.rename(nested)
    baseline = Layout(nested).shared_tree("snapshot")
    before = state.tree_digest(baseline)
    monkeypatch.setattr(state, "_rename_vault", lambda *args: pytest.fail("vault moved before topology validation"))
    with pytest.raises(state.StateError, match="disjoint"):
        state.build_arm(layout, arm_specs()["P"], tree="snapshot", source_vault=source,
                        parent_env={}, seed_build_from=nested)
    assert state.tree_digest(baseline) == before


def test_seed_preparation_cannot_bless_a_builder_write_to_the_original_vault(
    seed_build_round: tuple[Layout, Layout, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from membench.utility.switch_eval import state

    layout, donor, source = seed_build_round

    def built(*args, **kwargs):
        (layout.slot / "vault" / "page.md").write_text("Unexpected builder write.")
        return subprocess.CompletedProcess(args[0], 0, json.dumps({
            "vectors": {"state": "current"}, "serving_freshness": {"key_sha256": "fixture"}, "vault_digest": state.tree_digest(layout.slot / "vault"),
        }), "")

    monkeypatch.setattr(state.subprocess, "run", built)
    monkeypatch.setattr(state, "capture_surface", lambda *args, **kwargs: {})
    monkeypatch.setattr(state, "prove_quiescence", lambda *args, **kwargs: pytest.fail("changed vault was blessed"))
    with pytest.raises(state.StateError, match="vault digest changed"):
        state.build_arm(layout, arm_specs()["P"], tree="snapshot", source_vault=source,
                        parent_env={}, seed_build_from=donor.run)
    assert (layout.slot / "vault" / "page.md").read_text() == "Unexpected builder write."
    assert (layout.run / "slot-vault.json").is_file()
    assert not (layout.shared_tree("snapshot") / "state.json").exists()


@pytest.fixture
def failed_checkpoint(seed_build_round: tuple[Layout, Layout, Path]) -> tuple[Layout, Layout, Path]:
    """An archived failed proof retains state and dirty custody at the old serving identity."""
    from dataclasses import asdict

    from membench.utility.switch_eval import __main__ as cli
    from membench.utility.switch_eval import state

    from exomem.commands import product_commands_for, resolve_command

    layout, archive, source = seed_build_round
    baseline = archive.shared_tree("snapshot")
    (baseline / "state.json").unlink()
    archive.slot.mkdir()
    (baseline / "vault").rename(archive.slot / "vault")
    dirty = archive.slot / "vault"
    dirty.mkdir(parents=True, exist_ok=True)
    (dirty / "page.md").write_text("Dirty failed proof output.")
    (archive.run / "slot-vault.json").write_text('{"session":"failed","digest":"unmatched"}')
    (baseline / "build.stdout").write_text('{"vectors":{"state":"current"}}\n')
    (baseline / "build.stderr").write_text("Preserved publisher diagnostics.\n")
    (baseline / "quiescence.json").write_text('{"content_unchanged":false}')
    case = _case("checkpoint-case")
    key_file = source.parent / "key-input.jsonl"
    key_file.write_text(json.dumps(case.as_dict()) + "\n")
    case_file = source.parent / "case-input.jsonl"
    case_file.write_text(json.dumps({"case_id": case.case_id, "turns": case.turns}) + "\n")
    commands = product_commands_for("mcp")
    native_tools = [resolve_command(name, commands).name for name in protocol.READ_TOOLS]
    predecessor_protocol = protocol.snapshot()
    predecessor_protocol.update(
        READ_TOOLS=native_tools, ACTIVATION_TOOL=native_tools[0], P_FILTERED_TOOL=native_tools[0],
    )
    inputs = {
        "design": protocol.DESIGN, "protocol": predecessor_protocol,
        "harness_digest": "previous-harness", "arms": ["P"], "repeats": 1, "seed": 0,
        "hook_mode": arm_specs()["M"].hook_mode,
        "retrieval_profile": {"name": arm_specs()["P"].profile,
                              "settings": state.profile_settings(arm_specs()["P"].profile)},
        "actor_model": "fixture-model", "client": {"executable": sys.executable,
            "version": "fixture", "launcher": []},
        "trees": {"snapshot": {"path": str(source), "digest": state.tree_digest(source)}},
        "product_source": {"source_revision": "predecessor-product"},
        "keys": [{"split": case.split, "path": str(key_file), "sha256": cli._sha256_file(key_file)}],
        "case_files": [{"path": str(case_file), "sha256": cli._sha256_file(case_file)}],
        "case_ids": [case.case_id], "struck_before_freeze": [],
    }
    (archive.run / "inputs.json").write_text(json.dumps(inputs))
    (archive.run / "cases.json").write_text("  " + json.dumps([case.as_dict()]) + "\n")
    plan = plan_sessions([case], arm_ids=("P",), repeats=1, seed=0)
    (archive.run / "plan.json").write_text(json.dumps([asdict(item) for item in plan]) + "  \n")
    return layout, archive, source


def test_failed_checkpoint_import_preserves_opaque_inputs_and_earns_new_state_proof(
    failed_checkpoint: tuple[Layout, Layout, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from membench.utility.switch_eval import __main__ as cli
    from membench.utility.switch_eval import state
    from membench.utility.switch_eval.score import check_trees

    from exomem import state_paths

    layout, archive, source = failed_checkpoint
    predecessor_bytes = (archive.run / "inputs.json").read_bytes()
    before = state.tree_digest(archive.run)
    monkeypatch.setattr(state, "product_source_identity", lambda: {"source_revision": "current-product"})
    assert cli.main(["prepare", "--run", str(layout.run), "--import-checkpoint-from", str(archive.run)]) == 0
    for name in ("cases.json", "plan.json"):
        assert (layout.run / name).read_bytes() == (archive.run / name).read_bytes()
    inputs = json.loads((layout.run / "inputs.json").read_text())
    original = json.loads((archive.run / "inputs.json").read_text())
    assert {key: inputs[key] for key in original if key not in ("harness_digest", "product_source", "protocol")} == {
        key: value for key, value in original.items() if key not in ("harness_digest", "product_source", "protocol")
    }
    assert inputs["protocol"] == protocol.snapshot()
    projection = inputs["checkpoint_predecessor"]["discovery_projection"]
    assert projection["execution_names"] == original["protocol"]["READ_TOOLS"]
    assert projection["predecessor"]["READ_TOOLS"] == original["protocol"]["READ_TOOLS"]
    assert projection["predecessor"]["ACTIVATION_TOOL"] == original["protocol"]["ACTIVATION_TOOL"]
    assert projection["predecessor"]["P_FILTERED_TOOL"] == original["protocol"]["P_FILTERED_TOOL"]
    assert projection["current"]["READ_TOOLS"] == inputs["protocol"]["READ_TOOLS"]
    assert projection["current"]["ACTIVATION_TOOL"] == inputs["protocol"]["ACTIVATION_TOOL"]
    assert projection["current"]["P_FILTERED_TOOL"] == inputs["protocol"]["P_FILTERED_TOOL"]
    assert inputs["harness_digest"] == cli._harness_digest()
    assert inputs["product_source"] == {"source_revision": "current-product"}
    assert inputs["checkpoint_predecessor"]["predecessor_product_source"] == original["product_source"]
    assert not (layout.shared_tree("snapshot") / "state.json").exists()

    def built(*args, **kwargs):
        machine = layout.slot / "state" / state_paths.vault_state_key(layout.slot / "vault")
        assert (machine / "vectors.bin").read_bytes() == b"expensive compatible vectors"
        assert state.tree_digest(layout.slot / "vault") == state.tree_digest(source)
        (machine / "lexical.sqlite").write_bytes(b"fresh publication")
        return subprocess.CompletedProcess(args[0], 0, json.dumps({
            "vectors": {"state": "current", "coverage": {"reconstructed_parents": 1}}, "serving_freshness": {"key_sha256": "fixture"},
            "vault_digest": state.tree_digest(layout.slot / "vault"),
            "product_source": {"source_revision": "current-product"},
        }), "")

    monkeypatch.setattr(state.subprocess, "run", built)
    monkeypatch.setattr(state, "capture_surface", lambda *args, **kwargs: {"fresh": True})
    # Keep the real quiescence comparison; only the fresh-server transport is synthetic.
    monkeypatch.setattr(state, "_ask_surface", lambda *args, **kwargs: ("", []))
    assert cli.main(["state", "--run", str(layout.run), "--checkpoint-from", str(archive.run)]) == 0
    record = state.load_arm_state(layout, "P", "snapshot")
    state._require_build_proof(record)
    assert record["quiescence"]["content_unchanged"] is True
    assert record["product_source"] == record["build"]["product_source"] == {"source_revision": "current-product"}
    assert record["checkpoint"]["state_digest"] == state.tree_digest(archive.shared_tree("snapshot") / "state")
    assert "cpu_threads" not in record
    assert record["preparation"]["cpu_threads"] is None
    assert check_trees(layout, cases=[_case("checkpoint-case")], trees=inputs["trees"], arm_ids=["P"]) == []
    assert cli._stage_problems(layout, inputs, ["state"]) == []
    assert (archive.run / "inputs.json").read_bytes() == predecessor_bytes
    assert state.tree_digest(archive.run) == before


def test_interrupted_checkpoint_requires_explicit_retry_before_any_custody_or_cleanup(
    failed_checkpoint: tuple[Layout, Layout, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from membench.utility.switch_eval import __main__ as cli
    from membench.utility.switch_eval import state

    layout, archive, source = failed_checkpoint
    monkeypatch.setattr(state, "product_source_identity", lambda: {"source_revision": "fixture"})
    cli.main(["prepare", "--run", str(layout.run), "--import-checkpoint-from", str(archive.run)])
    before = state.tree_digest(archive.run)

    copytree = state.shutil.copytree

    def interrupted(source, destination, *args, **kwargs):
        result = copytree(source, destination, *args, **kwargs)
        if Path(destination) == layout.slot / "state":
            (Path(destination) / "partial").write_text("interrupted import")
            raise KeyboardInterrupt
        return result

    monkeypatch.setattr(state.shutil, "copytree", interrupted)
    with pytest.raises(KeyboardInterrupt):
        state.build_arm(layout, arm_specs()["P"], tree="snapshot", source_vault=source,
                        parent_env={}, checkpoint_from=archive.run)
    (layout.run / "slot-vault.json").write_text('{"unreadable":"partial custody"}')
    partial = state.tree_digest(layout.run)
    for build in (state.build_tree, state.build_arm):
        for selection in (None, source):
            with pytest.raises(state.StateError, match="--checkpoint-from|checkpoint selection"):
                build(layout, arm_specs()["P"], tree="snapshot", source_vault=source,
                      parent_env={}, checkpoint_from=selection)
            assert state.tree_digest(layout.run) == partial
    assert state.tree_digest(archive.run) == before

    monkeypatch.setattr(state.shutil, "copytree", copytree)

    def built(*args, **kwargs):
        assert not (layout.slot / "state" / "partial").exists()
        assert state.tree_digest(layout.slot / "state") == state.tree_digest(archive.shared_tree("snapshot") / "state")
        return subprocess.CompletedProcess(args[0], 0, json.dumps({
            "vectors": {"state": "current"}, "serving_freshness": {"key_sha256": "fixture"}, "vault_digest": state.tree_digest(layout.slot / "vault"),
            "product_source": {"source_revision": "fixture"},
        }), "")

    monkeypatch.setattr(state.subprocess, "run", built)
    monkeypatch.setattr(state, "capture_surface", lambda *args, **kwargs: {})
    monkeypatch.setattr(state, "_ask_surface", lambda *args, **kwargs: ("", []))
    state.build_arm(layout, arm_specs()["P"], tree="snapshot", source_vault=source,
                    parent_env={}, checkpoint_from=archive.run)
    assert state.tree_digest(archive.run) == before


@pytest.mark.parametrize("topology", ["equal", "nested_successor", "nested_archive", "alias", "frozen_source"])
def test_checkpoint_topology_refuses_before_it_can_delete_evidence(
    failed_checkpoint: tuple[Layout, Layout, Path], topology: str,
) -> None:
    from membench.utility.switch_eval import state

    layout, archive, source = failed_checkpoint
    if topology == "equal":
        layout = archive
    elif topology == "nested_successor":
        layout = Layout(archive.run / "successor")
    elif topology == "nested_archive":
        destination = layout.slot / "archive"
        destination.parent.mkdir(parents=True)
        archive.run.rename(destination)
        archive = Layout(destination)
    elif topology == "alias":
        layout.run.symlink_to(archive.run, target_is_directory=True)
    else:
        destination = archive.run / "frozen-source"
        source.rename(destination)
        path = archive.run / "inputs.json"
        inputs = json.loads(path.read_text())
        inputs["trees"]["snapshot"]["path"] = str(destination)
        path.write_text(json.dumps(inputs))
    before = state.tree_digest(archive.run)
    with pytest.raises(state.StateError, match="disjoint|frozen source must be outside"):
        state.prepare_checkpoint_import(layout, archive.run, harness_digest="fixture")
    assert state.tree_digest(archive.run) == before


def test_surface_capture_cannot_bless_a_write_after_final_vector_publication(
    failed_checkpoint: tuple[Layout, Layout, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from membench.utility.switch_eval import __main__ as cli
    from membench.utility.switch_eval import state

    layout, archive, source = failed_checkpoint
    monkeypatch.setattr(state, "product_source_identity", lambda: {"source_revision": "fixture"})
    cli.main(["prepare", "--run", str(layout.run), "--import-checkpoint-from", str(archive.run)])
    before = state.tree_digest(archive.run)
    monkeypatch.setattr(state.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(
        args[0], 0, json.dumps({
            "vectors": {"state": "current"}, "serving_freshness": {"key_sha256": "fixture"}, "vault_digest": state.tree_digest(layout.slot / "vault"),
            "product_source": {"source_revision": "fixture"},
        }), "",
    ))

    def surface(slot, *args, **kwargs):
        (slot.vault / "page.md").write_text("Unexpected surface-capture write.")
        return {}

    monkeypatch.setattr(state, "capture_surface", surface)
    with pytest.raises(state.StateError, match="surface capture changed"):
        state.build_arm(layout, arm_specs()["P"], tree="snapshot", source_vault=source,
                        parent_env={}, checkpoint_from=archive.run)
    assert not (layout.shared_tree("snapshot") / "state.json").exists()
    assert not (layout.arm_tree("P", "snapshot") / "state.json").exists()
    assert state.tree_digest(archive.run) == before


@pytest.mark.parametrize("changed", ["protocol", "retrieval_profile", "invalid_tool", "unrelated_tool"])
def test_checkpoint_import_refuses_a_changed_experimental_contract(
    failed_checkpoint: tuple[Layout, Layout, Path], changed: str,
) -> None:
    from membench.utility.switch_eval import state

    layout, archive, _source = failed_checkpoint
    path = archive.run / "inputs.json"
    inputs = json.loads(path.read_bytes())
    if changed == "protocol":
        inputs["protocol"]["PULL_BUDGET_PER_TURN"] = protocol.PULL_BUDGET_PER_TURN // 2
    elif changed == "retrieval_profile":
        del inputs["retrieval_profile"]["settings"]["EXOMEM_LEXICAL_BACKEND"]
    elif changed == "invalid_tool":
        inputs["protocol"]["READ_TOOLS"][0] = "unknown-command"
    else:
        from exomem.commands import product_commands_for

        other = next(command for command in product_commands_for("mcp")
                     if command.name not in inputs["protocol"]["READ_TOOLS"])
        inputs["protocol"]["READ_TOOLS"][0] = other.public_name
    path.write_text(json.dumps(inputs))
    before = state.tree_digest(archive.run)
    with pytest.raises(state.StateError, match="protocol|retrieval settings"):
        state.prepare_checkpoint_import(layout, archive.run, harness_digest="fixture")
    assert not layout.run.exists()
    assert state.tree_digest(archive.run) == before


@pytest.mark.parametrize("handoff", ["missing", "changed"])
def test_parent_requires_the_childs_exact_final_coverage_identity(
    failed_checkpoint: tuple[Layout, Layout, Path], monkeypatch: pytest.MonkeyPatch,
    handoff: str,
) -> None:
    from membench.utility.switch_eval import __main__ as cli
    from membench.utility.switch_eval import state

    layout, archive, source = failed_checkpoint
    monkeypatch.setattr(state, "product_source_identity", lambda: {"source_revision": "fixture"})
    cli.main(["prepare", "--run", str(layout.run), "--import-checkpoint-from", str(archive.run)])
    before = state.tree_digest(archive.run)

    def built(*args, **kwargs):
        report = {"vectors": {"state": "current"}, "serving_freshness": {"key_sha256": "fixture"}, "product_source": {"source_revision": "fixture"}}
        if handoff == "changed":
            report["vault_digest"] = state.tree_digest(layout.slot / "vault")
            (layout.slot / "vault" / "page.md").write_text("A write after the child proved coverage.")
        return subprocess.CompletedProcess(args[0], 0, json.dumps(report), "")

    monkeypatch.setattr(state.subprocess, "run", built)
    monkeypatch.setattr(state, "capture_surface", lambda *args, **kwargs: pytest.fail("unproven coverage reached surface capture"))
    with pytest.raises(state.StateError, match="coverage identity|changed after final vector coverage"):
        state.build_arm(layout, arm_specs()["P"], tree="snapshot", source_vault=source,
                        parent_env={}, checkpoint_from=archive.run)
    assert not (layout.shared_tree("snapshot") / "state.json").exists()
    assert not (layout.arm_tree("P", "snapshot") / "state.json").exists()
    assert state.tree_digest(archive.run) == before


@pytest.mark.parametrize("observed", ["missing", "different"])
def test_checkpoint_requires_the_builders_observed_product_source(
    failed_checkpoint: tuple[Layout, Layout, Path], monkeypatch: pytest.MonkeyPatch,
    observed: str,
) -> None:
    from membench.utility.switch_eval import __main__ as cli
    from membench.utility.switch_eval import state

    layout, archive, source = failed_checkpoint
    monkeypatch.setattr(state, "product_source_identity", lambda: {"source_revision": "expected-parent-source"})
    cli.main(["prepare", "--run", str(layout.run), "--import-checkpoint-from", str(archive.run)])
    before = state.tree_digest(archive.run)

    def built(*args, **kwargs):
        report = {"vectors": {"state": "current"}, "serving_freshness": {"key_sha256": "fixture"}, "vault_digest": state.tree_digest(layout.slot / "vault")}
        if observed == "different":
            report["product_source"] = {"source_revision": "observed-child-source"}
        return subprocess.CompletedProcess(args[0], 0, json.dumps(report), "")

    monkeypatch.setattr(state.subprocess, "run", built)
    monkeypatch.setattr(state, "capture_surface", lambda *args, **kwargs: pytest.fail("unverified builder source reached surface capture"))
    with pytest.raises(state.StateError, match="builder product source"):
        state.build_arm(layout, arm_specs()["P"], tree="snapshot", source_vault=source,
                        parent_env={}, checkpoint_from=archive.run)
    assert not (layout.shared_tree("snapshot") / "state.json").exists()
    assert not (layout.arm_tree("P", "snapshot") / "state.json").exists()
    assert state.tree_digest(archive.run) == before


def test_a_state_built_before_the_serving_stamps_is_refused_for_reuse() -> None:
    from membench.utility.switch_eval import state

    record = {
        "build": {"vectors": {"state": "current"}},
        "quiescence": {"content_unchanged": True, "signatures_unchanged": True},
        "vault_signature_digest": "fixture",
    }
    with pytest.raises(state.StateError, match="stamped serving freshness"):
        state._require_build_proof(record)
