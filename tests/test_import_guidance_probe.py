"""The probe's own branching logic: scripted-reply choice, deterministic checks, the verdict."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from import_guidance import checks, judge, runner
from import_guidance.scripted_replies import Scenario, pick_reply

from exomem.cloud_import_windows import Figure, Reading

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


def test_observe_transfer_without_an_action_gets_the_status_reply() -> None:
    """Catches a legal call that omits `action` (the schema default is `status`) getting the harness's error reply."""
    [scenario_04] = runner.load_scenarios("04")
    status = scenario_04.replies["observe_transfer.status"]
    assert pick_reply(scenario_04, "observe_transfer", {}, 0) == status
    assert pick_reply(scenario_04, "observe_transfer", {"import_id": IMP}, 0) == status


# The status replies are in the shape the product returns.

# What `Figure.of` says about a value that is not a figure: the reader's own words, not a copy of them here.
NOT_A_FIGURE = Figure.of(7).why


def test_every_scenario_status_reads_through_the_product_reader() -> None:
    """Catches a scripted status in a shape the tool never returns (a bare-integer figure, a string classification,
    counts that disagree with the listed windows), which the real rounds would then judge the agent against."""
    for scenario in runner.load_scenarios("all"):
        reply = scenario.replies["read_import.status"]
        reading = Reading.of(reply)
        assert not reading.older_cell and reading.windows_unknown is None, scenario.id
        figures = (
            reading.budget,
            reading.reachable,
            reading.pinned,
            reading.effective,
            reading.allowance,
        )
        assert all(f.bytes is not None or f.why != NOT_A_FIGURE for f in figures), scenario.id
        assert reading.volume_used.bytes is not None and reading.bounds is not None, scenario.id
        assert reading.largest_offloadable == reading.bounds.largest_offloadable(), scenario.id
        if None not in (reading.reachable.bytes, reading.pinned.bytes, reading.effective.bytes):
            room = reading.reachable.bytes + reading.pinned.bytes
            assert reading.effective.bytes == max(0, min(reading.largest_offloadable, room)), (
                scenario.id
            )
        listing = reply.get("import_windows", {"counts": {}, "windows": []})
        states = [window["state"] for window in listing["windows"]]
        assert (
            listing["counts"]
            == {state: states.count(state) for state in ("landed", "discarded", "unreadable")}
            or not states
        ), scenario.id
        for window in listing["windows"]:
            listed = len(window.get("members", ()))
            assert (
                listed
                == (window.get("unfiled") or window.get("residual") or {"count": listed})["count"]
            )


# Whole runs through the injected `run`, with no model.

SERVER_UP = [{"name": "exomem", "status": "connected"}]


def stream(
    servers: list | None, text: str = "Here is the answer.", tools: tuple[str, ...] = ()
) -> str:
    events: list[dict] = (
        [] if servers is None else [{"type": "system", "subtype": "init", "mcp_servers": servers}]
    )
    if tools:
        events.append(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": n, "input": {}} for n in tools]
                },
            }
        )
    events.append(
        {"type": "result", "subtype": "success", "is_error": False, "result": text, "num_turns": 2}
    )
    return "\n".join(json.dumps(event) for event in events) + "\n"


class FakeClaude:
    """Stands in for `claude -p`. The agent call logs `logged` the way the scripted server does and prints `stdout`;
    the judge call answers every criterion as the scenario expects."""

    def __init__(
        self, scenario: Scenario, stdout: str, logged: list[dict] | None = None, returncode: int = 0
    ) -> None:
        self.scenario, self.stdout, self.logged, self.returncode = (
            scenario,
            stdout,
            logged or [],
            returncode,
        )
        self.agent_cwd: Path | None = None
        self.seen_at_start: dict[str, bool] = {}
        self.agent_calls = 0

    def __call__(
        self, argv: list[str], cwd: Path, env: dict, timeout: float
    ) -> subprocess.CompletedProcess:
        if "--json-schema" in argv:
            answer = {c["id"]: c["expect"] for c in self.scenario.criteria}
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"structured_output": answer}), ""
            )
        self.agent_calls += 1
        self.agent_cwd = cwd
        config = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())
        call_log = Path(config["mcpServers"]["exomem"]["env"]["IG_CALL_LOG"])
        self.seen_at_start = {
            "calls": call_log.exists(),
            "shell": Path(env["IG_SHELL_LOG"]).exists(),
        }
        with call_log.open("a", encoding="utf-8") as handle:
            for entry in self.logged:
                handle.write(json.dumps(entry) + "\n")
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


def drive(scenario: Scenario, fake: FakeClaude, out: Path, judge_model: str = "judge-m") -> dict:
    return runner.run_once(
        scenario,
        repeat=0,
        model="agent-m",
        judge_model=judge_model,
        out=out,
        executable=Path("claude"),
        run=fake,
    )


def test_a_second_run_into_the_same_folder_does_not_pass_on_the_first_runs_calls(
    tmp_path: Path,
) -> None:
    """Catches stale evidence: the server appends to `calls.jsonl`, so a run that never acknowledged
    would read the earlier run's `acknowledge` and pass."""
    [scenario_03] = runner.load_scenarios("03")
    acknowledge = {
        "t": 1.0,
        "tool": "import_memory",
        "kind": "acknowledge",
        "arguments": {
            "operation": {
                "kind": "acknowledge",
                "import_id": "5b0e9c1e-7f1d-4a39-9d52-2f6c1d0a8b11",
                "paths": ["Scans/receipt-2019.pdf", "Misc/backup.dmg"],
            }
        },
    }
    first = FakeClaude(
        scenario_03, stream(SERVER_UP, tools=("mcp__exomem__import_memory",)), [acknowledge]
    )
    second = FakeClaude(scenario_03, stream(SERVER_UP))
    assert drive(scenario_03, first, tmp_path)["verdict"] == "pass"
    (tmp_path / "work" / "03-clear-residual-specific-0" / "shell.log").write_text(
        "exit 1\n", encoding="utf-8"
    )

    assert drive(scenario_03, second, tmp_path)["verdict"] == "fail"
    assert second.seen_at_start == {"calls": False, "shell": False}


@pytest.mark.parametrize(
    "servers",
    [[{"name": "exomem", "status": "failed"}], None],
    ids=["server failed", "no init event"],
)
def test_a_run_without_a_connected_scripted_server_is_unknown_not_pass(
    tmp_path: Path, servers: list | None
) -> None:
    """Catches a pass from the skill text alone: scenario 01 needs no tool call, so an agent with no tools passed."""
    [scenario_01] = runner.load_scenarios("01")
    row = drive(scenario_01, FakeClaude(scenario_01, stream(servers)), tmp_path)
    assert row["verdict"] == "unknown"
    assert "exomem" in row["error"] or "init" in row["error"]


def test_an_agent_that_exits_non_zero_is_unknown_even_with_a_full_transcript(
    tmp_path: Path,
) -> None:
    """Catches a crashed or rate-limited agent whose partial transcript still reads as a pass."""
    [scenario_01] = runner.load_scenarios("01")
    row = drive(scenario_01, FakeClaude(scenario_01, stream(SERVER_UP), returncode=1), tmp_path)
    assert row["verdict"] == "unknown" and "exit code 1" in row["error"]


def test_a_timed_out_agent_is_unknown_and_never_asks_the_judge(tmp_path: Path) -> None:
    """Catches a timeout path that scores a half-written log or reaches for the judge."""
    [scenario_01] = runner.load_scenarios("01")
    calls: list[list[str]] = []

    def times_out(argv: list[str], cwd: Path, env: dict, timeout: float):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, timeout)

    row = runner.run_once(
        scenario_01,
        repeat=0,
        model="m",
        judge_model="j",
        out=tmp_path,
        executable=Path("claude"),
        run=times_out,
    )
    assert (row["verdict"], len(calls)) == ("unknown", 1)
    assert row["error"].startswith("TimeoutExpired")


def test_a_scored_run_starts_in_an_empty_folder_and_records_the_text_under_test(
    tmp_path: Path,
) -> None:
    """Catches harness files (the scenario path in `mcp.json` is the answer key) inside the agent's folder,
    and a row that cannot be tied to the skill and tool text the agent saw."""
    [scenario_01] = runner.load_scenarios("01")
    fake = FakeClaude(scenario_01, stream(SERVER_UP))
    row = drive(scenario_01, fake, tmp_path, judge_model="judge-m")

    assert row["verdict"] == "pass"
    assert fake.agent_cwd is not None and list(fake.agent_cwd.iterdir()) == []
    assert (fake.agent_cwd.parent / "mcp.json").exists()
    assert row["judge_model"] == "judge-m"
    assert len(row["skill_sha256"]) == len(row["tool_schemas_sha256"]) == 64


def test_the_agent_environment_has_no_exomem_variable_but_keeps_home(tmp_path: Path) -> None:
    """Catches a real vault path reaching the agent's shell through an inherited `EXOMEM_*` variable."""
    parent = {"EXOMEM_VAULT_PATH": "/real/vault", "HOME": "/home/someone", "PATH": "/usr/bin"}
    env = runner.child_env(parent, tmp_path / "bin", tmp_path / "shell.log")
    assert "EXOMEM_VAULT_PATH" not in env and env["HOME"] == "/home/someone"


def test_the_fake_exomem_records_no_import_for_a_command_that_is_not_the_import(
    tmp_path: Path,
) -> None:
    """Catches `exomem --version` blocking for the long import and writing the `exit` event that makes
    `filed_before_shell_exit` fail a run that filed in time."""
    [scenario_07] = runner.load_scenarios("07")
    runner.install_fake_commands(scenario_07, tmp_path / "bin")
    log = tmp_path / "shell.log"
    done = subprocess.run(
        [str(tmp_path / "bin" / "exomem"), "--version"],
        env={"IG_SHELL_LOG": str(log), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert done.returncode == 0 and not log.exists()
