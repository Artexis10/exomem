"""The outcome evaluation harness measures what it says it measures.

Failures a green run would not reveal: an actor that reaches the live vault, a judge that can
tell which arm wrote an answer, a scorer that disagrees with the upstream key, a judge verdict
that is really a stale file, a report that counts a faulted run, and an arm that gains by not
answering.
"""

from __future__ import annotations

import json
import re
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from epistemic.journeys.f27_replay import PLUGIN_DIR, AgentEnvelope
from membench.outcome.clbench import report as clbench_report
from membench.outcome.clbench.run import ARMS, CellSpec, ClaudeActor, QuestionLog, run_cell
from membench.outcome.clbench.run import UPSTREAM as CLBENCH_UPSTREAM
from membench.outcome.command import write_record
from membench.outcome.faults import LimitRejected, init_faults
from membench.outcome.isolation import child_env, mcp_server, server_pins
from membench.outcome.judging import Judge, JudgeSpec
from membench.outcome.lab import judge as lab_judge
from membench.outcome.lab import run as lab_run
from membench.outcome.lab.prepare import CASES, slug
from membench.outcome.lab.prepare import UPSTREAM as LAB_UPSTREAM
from membench.outcome.lab.run import FILE_ALLOWS, SHIPPED_ALLOWS, Lab
from membench.outcome.planted import answer as planted_answer
from membench.outcome.planted import build as planted_build
from membench.outcome.planted import judge as planted_judge
from membench.outcome.planted.answer import answer_actor, answer_all
from membench.outcome.stats import paired_interval
from membench.outcome.workspace import head_of, pin

# What the operator's real service and vault would put in the environment of a shell that runs the harness.
LIVE_ENV = {
    "EXOMEM_VAULT_PATH": "/live/vault",
    "EXOMEM_STATE_ROOT": "/live/state",
    "EXOMEM_CONFIG_PATH": "/live/config.json",
    "EXOMEM_SERVICE_ENV": "/live/service.env",
    "EXOMEM_HOOK_HOME": "/live/hook-home",
    "EXOMEM_HOST": "live.example",
    "KB_VAULT_PATH": "/live/kb-vault",
}
LIVE_XDG = "/live/xdg-state"
ENVELOPE = AgentEnvelope(executable=Path("/usr/bin/claude-under-test"), version="0.0-test")


def paths_outside(env: dict[str, str], root: Path) -> list[str]:
    """The Exomem settings in ``env`` that are absolute paths outside ``root``."""
    return [
        f"{key}={value}"
        for key, value in env.items()
        if key.startswith("EXOMEM_")
        and Path(value).is_absolute()
        and not Path(value).is_relative_to(root)
    ]


@pytest.mark.reads_repository_docs  # the shipped arms read the instruction block out of docs/prominence.md
def test_actor_environment_cannot_reach_live_state(tmp_path, monkeypatch):
    """An actor started from a shell that has a live Exomem set up must not write to or read from it."""
    for name, value in LIVE_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("XDG_STATE_HOME", LIVE_XDG)

    # Per surface: its directory, the environments the actor runs with, the Exomem servers' own
    # environments (which also fix the state root), the actor's argv and its MCP config file.
    surfaces = {}
    for arm in ("P", "E", "EH"):
        arm_dir = tmp_path / arm
        actor = ClaudeActor(
            ARMS[arm], arm_dir, "model-under-test", "high", "memory {memory_dir}", envelope=ENVELOPE
        )
        server = json.loads(actor.mcp_config.read_text())["mcpServers"]["exomem"]["env"]
        surfaces[f"clbench {arm}"] = (
            arm_dir,
            [actor.session_env(0), actor.session_env(1)],
            [server],
            actor.argv(),
            actor.mcp_config,
        )
    # LAB's shipped arm: the compile and the task session each start the plugin's hooks.
    monkeypatch.setattr(lab_run, "RUN_ROOT", tmp_path)
    lab_dir = tmp_path / "runs" / "lab-run"
    lab_dir.mkdir(parents=True)
    lab = Lab(lab_dir, ["matter"], "model-under-test", "high", envelope=ENVELOPE)
    wp_dir = lab_dir / "matter" / "WP"
    surfaces["lab WP"] = (
        lab_dir,
        [lab.env_for(wp_dir, session, shipped=True) for session in ("compile", "task")],
        [mcp_server(wp_dir)["env"]],
        lab.argv(
            wp_dir / "mcp.json",
            [*FILE_ALLOWS, *SHIPPED_ALLOWS],
            lab.protected("matter", "WP"),
            shipped=True,
        ),
        None,
    )
    answer_dir = tmp_path / "planted-E"
    argv, env, mcp_config = answer_actor(answer_dir, "model-under-test", "high", ENVELOPE)
    server = json.loads(mcp_config.read_text())["mcpServers"]["exomem"]["env"]
    surfaces["planted E"] = (answer_dir, [env], [server], argv, mcp_config)
    pins = tmp_path / "pins"
    surfaces["pins"] = (pins, [child_env(pins)], [server_pins(pins)], [], None)
    judge = Judge(JudgeSpec.of("claude", executable=str(ENVELOPE.executable)), tmp_path / "judge")
    surfaces["judge"] = (
        tmp_path / "judge",
        [judge.env],
        [],
        judge.argv({}, tmp_path / "out"),
        None,
    )

    for name, (root, envs, servers, argv, mcp_config) in surfaces.items():
        reachable = json.dumps([envs, servers, argv])
        for setting, value in LIVE_ENV.items():
            assert value not in reachable, f"{name} carries the live {setting}"
        assert LIVE_XDG not in json.dumps(servers), (
            f"{name}'s Exomem server would use the live state root"
        )
        for env in [*envs, *servers]:
            assert paths_outside(env, root) == [], f"{name} points Exomem outside its own directory"
        if mcp_config:
            assert "--strict-mcp-config" in argv, (
                f"{name} would also load the operator's MCP servers"
            )
            assert Path(argv[argv.index("--mcp-config") + 1]).is_relative_to(root)
    # A hook is a child of the actor: its home must be the arm's own. So must the instruction block.
    for name in ("clbench P", "clbench EH", "lab WP"):
        root, envs, _servers, argv, _config = surfaces[name]
        assert "--plugin-dir" in argv, f"{name} would not load the plugin"
        homes = [Path(env["EXOMEM_HOOK_HOME"]) for env in envs]
        assert all(home.is_relative_to(root) for home in homes), name
        # Each question or session is a separate user session: no shared cooldown stamps.
        assert len(set(homes)) == len(homes), f"{name} shares one hook home"
    for name in ("clbench P", "lab WP"):
        root, _envs, _servers, argv, _config = surfaces[name]
        block = Path(argv[argv.index("--append-system-prompt-file") + 1])
        assert block.is_relative_to(root), f"{name} reads its instruction block from outside"
    assert "--plugin-dir" not in surfaces["clbench E"][3], "the E ablation would load the plugin"
    # A skill reads its own references/ files, so the plugin directory is readable. That is the
    # only read outside the workspace, and the protected paths stay disallowed.
    protected = {
        "clbench P": [CLBENCH_UPSTREAM],
        "clbench EH": [CLBENCH_UPSTREAM],
        "lab WP": [LAB_UPSTREAM, CASES.parent],
    }
    for name, paths in protected.items():
        argv = surfaces[name][3]
        allowed = argv[argv.index("--allowedTools") + 1].split(",")
        disallowed = argv[argv.index("--disallowedTools") + 1].split(",")
        reads = {rule for rule in allowed if rule.startswith("Read(")}
        assert reads == {"Read(./**)", f"Read(/{PLUGIN_DIR}/**)"}, f"{name} reads {reads}"
        for path in paths:
            assert f"Read(/{path}/**)" in disallowed, f"{name} may read {path}"


def test_judge_input_never_names_the_arm(tmp_path, monkeypatch):
    """A judge that can tell the arms apart would grade the arm, not the work."""
    monkeypatch.setattr(lab_judge, "RUN_ROOT", tmp_path)
    prepared, run = tmp_path / "runs" / "prepared", tmp_path / "runs" / "lab-run"
    tasks = ["Matter One", "Matter Two"]
    lab_arms = ("S", "W", "WG")
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(
        json.dumps({"tasks": tasks, "prepared": "prepared", "arms": lab_arms})
    )
    for task in tasks:
        case_dir = prepared / "tasks" / slug(task)
        case_dir.mkdir(parents=True)
        case_dir.joinpath("case.json").write_text(
            json.dumps(
                {
                    "instructions": f"Advise on {task}.",
                    "criteria": [{"id": "c1", "match_criteria": "names the deadline"}],
                }
            )
        )
        for arm in lab_arms:
            arm_dir = run / slug(task) / arm
            arm_dir.mkdir(parents=True)
            arm_dir.joinpath("result.json").write_text(
                json.dumps(
                    {"faults": [], "harness_faults": [], "deliverable": {"file": "deliverable.md"}}
                )
            )
            arm_dir.joinpath("deliverable.md").write_text(f"The deadline in {task} is 30 days.")
    lab_run = run / "judge-claude"
    lab_judge.items(lab_run, ["lab-run"], list(lab_arms))
    seen = [
        lab_judge.build_prompt(json.loads(line)) + line
        for line in (lab_run / "judge" / "items.jsonl").read_text().splitlines()
    ]

    planted_arms = ("E", "P")
    planted_run = tmp_path / "planted-run"
    cases = [
        {"case_id": f"c{i}", "task": f"Plan {i}.", "key": {"correct": "14 days", "stale": "7 days"}}
        for i in range(3)
    ]
    for arm in planted_arms:
        answers = planted_run / arm / "answers"
        answers.mkdir(parents=True)
        for case in cases:
            answers.joinpath(f"{case['case_id']}.json").write_text(
                json.dumps(
                    {
                        "answer": f"Use 14 days for {case['case_id']}.",
                        "log": {"harness_faults": []},
                    }
                )
            )
    planted_judge.items(cases, planted_run, planted_run / "judge-claude", list(planted_arms))
    seen += [
        planted_judge.build_prompt(json.loads(line)) + line
        for line in (planted_run / "judge-claude" / "judge" / "items.jsonl")
        .read_text()
        .splitlines()
    ]

    assert len(seen) == len(tasks) * len(lab_arms) + len(cases) * len(planted_arms)
    assert all("deadline" in s or "14 days" in s for s in seen), "the judge must still see the work"
    leaks = [
        str(tmp_path),
        "lab-run",
        "planted-run",
        "vault",
        "unblinding",
        *(f"/{a}/" for a in (*lab_arms, *planted_arms)),
    ]
    for text in seen:
        for leak in leaks:
            assert leak not in text, f"a judge input contains {leak!r}"
        # The synthetic work never uses an arm code as a word, so one as a word was added by the harness.
        assert not {*lab_arms, *planted_arms} & set(re.findall(r"[A-Za-z0-9]+", text)), (
            "a judge input names an arm"
        )


def test_clbench_scorer_agrees_with_an_oracle_replay(tmp_path):
    """The upstream scorer gives full marks to the key and none to a wrong answer, so rewards mean what they say."""
    upstream = pin("clbench").checkout()
    if (
        head_of(upstream) != pin("clbench").commit
        or not (upstream / ".venv" / "bin" / "python").exists()
    ):
        pytest.skip(f"no pinned CL-Bench checkout at {upstream}; run `membench outcome fetch`")
    rewards = {}
    for actor in ("oracle", "wrong"):
        arm_dir = run_cell(CellSpec(run_dir=tmp_path / actor, arm=actor, limit=3))
        rows = [json.loads(line) for line in (arm_dir / "outcomes.jsonl").read_text().splitlines()]
        rewards[actor] = [row["outcome"]["reward"] for row in rows]
    assert rewards["oracle"] == [1.0] * 3
    assert rewards["wrong"] == [0.0] * 3


# --------------------------------------------------------------------------
# What a decision rests on.
# --------------------------------------------------------------------------


def stub_cli(path: Path, *, exit_code: int) -> str:
    """A judge CLI that reads its prompt, writes no verdict file and exits with ``exit_code``."""
    path.write_text(f"#!/bin/sh\ncat > /dev/null\nexit {exit_code}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


@pytest.mark.parametrize("exit_code", [0, 3])
def test_codex_judge_never_reads_an_old_verdict_file_as_its_own(tmp_path, exit_code):
    """A verdict file left by an earlier call must not pass as the verdict of a call whose CLI wrote none."""
    spec = JudgeSpec.of("codex", executable=stub_cli(tmp_path / "codex", exit_code=exit_code))
    out = tmp_path / "it-0001.verdicts.0.codex-out.json"
    out.write_text(json.dumps({"stale_trusted": False, "correct": True}))
    call = Judge(spec, tmp_path / "judge").call("prompt", {}, out=out, timeout=30)
    assert call.verdict is None and call.error


@pytest.mark.parametrize(
    ("init", "fault"),
    [
        ({"model": "claude-other", "apiKeySource": "none"}, "model"),
        ({"model": "m", "apiKeySource": "ANTHROPIC_API_KEY"}, "credential"),
        (
            {
                "model": "m",
                "apiKeySource": "none",
                "mcp_servers": [{"name": "exomem", "status": "failed"}],
            },
            "connected",
        ),
    ],
)
def test_a_session_that_did_not_run_as_configured_is_a_harness_fault(init, fault):
    """A fallback model, an API key or an Exomem server that never connected must void the unit, not score as a loss."""
    assert any(fault in text for text in init_faults(init, model="m", exomem=True))


def test_fewer_than_two_paired_units_is_never_better():
    """One paired unit makes a bootstrap interval a single point, which would read as a win."""
    assert paired_interval([0.5], seed=1, unit="question")["verdict"].startswith("insufficient")


def clbench_cell(
    run_dir: Path, arm: str, run_index: int, rewards: dict[str, float], faulty=()
) -> None:
    cell = run_dir / f"{arm}-r{run_index}"
    cell.mkdir(parents=True)
    manifest = {"arm": arm, "task": {"run_index": run_index, "instances": len(rewards)}}
    (cell / "manifest.json").write_text(json.dumps(manifest))
    rows = []
    for index, (question, reward) in enumerate(rewards.items()):
        log = QuestionLog(question_id=question, index=index, turns=1)
        if question in faulty:
            log.harness_faults.append("the CLI exited with 1 before it answered")
        outcome = {"reward": reward, "success": reward > 0, "metadata": {"num_queries": 1}}
        rows.append(json.dumps({"outcome": outcome, "log": log.__dict__}))
    (cell / "outcomes.jsonl").write_text("\n".join(rows) + "\n")
    # upstream's evaluation, or the marker a voided cell leaves instead (see run_cell)
    evaluation = {"voided": {"question_id": faulty[0]}} if faulty else {"score": 0.5}
    (cell / "evaluation.json").write_text(json.dumps(evaluation))


def test_a_voided_clbench_run_leaves_the_figures_and_run_index_4_replaces_it_once(tmp_path):
    """A run with a harness fault in any arm must not count, and its replacement is run_index 4 only."""
    questions = [str(i) for i in range(1, 7)]
    for run_index in (1, 2, 3):
        clbench_cell(tmp_path, "N", run_index, dict.fromkeys(questions, 0.0))
        # Run 2's P cell has a harness fault on one question: the whole run is void for every arm.
        faulty = ("3",) if run_index == 2 else ()
        clbench_cell(tmp_path, "P", run_index, dict.fromkeys(questions, 1.0), faulty=faulty)

    built = clbench_report.build(tmp_path, [("P", "N")], scheduled=[1, 2, 3], heldout=True)
    assert built["runs"]["valid"] == [1, 3] and list(built["runs"]["voided"]) == ["2"]
    assert built["decision"]["status"].startswith("incomplete")
    assert [r["run_index"] for r in built["pairs"]["P-N"]["per_run_mean_difference"]] == [1, 3]

    clbench_cell(tmp_path, "N", 4, dict.fromkeys(questions, 0.0))
    clbench_cell(tmp_path, "P", 4, dict.fromkeys(questions, 1.0))
    built = clbench_report.build(tmp_path, [("P", "N")], scheduled=[1, 2, 3, 4], heldout=True)
    assert built["runs"]["replacement"] == {"run_index": 4, "replaces": 2}
    assert built["runs"]["valid"] == [1, 3, 4] and built["decision"]["status"] == "complete"
    assert built["arms"]["P"]["units"]["missing"] == 0

    # A cell that ended before its evaluation leaves the decision undecided and says which cell.
    (tmp_path / "P-r4" / "evaluation.json").unlink()
    built = clbench_report.build(tmp_path, [("P", "N")], scheduled=[1, 2, 3, 4], heldout=True)
    assert built["decision"]["status"] == "incomplete"
    assert any("P run 4" in reason for reason in built["decision"]["why"])


VERDICT = {
    "value_used": "correct",
    "stale_trusted": False,
    "correct": True,
    "flags_conflict": False,
    "rationalises": False,
    "reason": "",
}


def planted_cases(count: int) -> list[dict]:
    key = {"correct": "14 days", "stale": "7 days"}
    return [
        {"case_id": f"c{i}", "domain": "contracts", "task": "Plan.", "key": key}
        for i in range(count)
    ]


def planted_answers(
    run: Path,
    case_ids: list[str],
    unanswered: dict[str, set[str]],
    arms: tuple[str, ...] = ("E", "P"),
) -> None:
    """Answers for the ``arms``; ``unanswered[arm]`` are the cases the arm gave no reply to."""
    for arm in arms:
        answers = run / arm / "answers"
        answers.mkdir(parents=True, exist_ok=True)
        for case_id in case_ids:
            reply = None if case_id in unanswered.get(arm, ()) else f"Use 14 days for {case_id}."
            answers.joinpath(f"{case_id}.json").write_text(
                json.dumps({"answer": reply, "log": QuestionLog(case_id, 0).__dict__})
            )


def test_an_arm_that_answers_nothing_cannot_read_as_protected(tmp_path):
    """An unanswered case counts as a stale answer, so governance cannot win by leaving the agent silent."""
    cases = planted_cases(8)
    case_ids = [c["case_id"] for c in cases]
    planted_answers(tmp_path / "run", case_ids, unanswered={"E": set(case_ids)})
    judge_run = tmp_path / "run" / "judge-claude"
    planted_judge.items(cases, tmp_path / "run", judge_run, ["E", "P"])
    ids = [
        json.loads(line)["id"]
        for line in (judge_run / "judge" / "items.jsonl").read_text().splitlines()
    ]
    assert len(ids) == len(case_ids), "only the answered cases go to the judge"
    (judge_run / "judge" / "verdicts.jsonl").write_text(
        "".join(json.dumps({"id": i, "verdict": VERDICT}) + "\n" for i in ids)
    )
    (judge_run / "judge" / "judge.json").write_text(
        json.dumps({"backend": "codex", "model": "gpt-6.1-sol"})
    )

    report = planted_judge.score(judge_run, [("E", "P")], heldout=True)

    assert report["arms"]["E"]["unanswered"] == len(case_ids)
    assert report["pairs"]["E-P"]["stale_trusted"]["mean_difference"] == 1.0
    assert report["decision"]["governance_protects"]["result"] is False


def verdict_cli(path: Path) -> str:
    """A judge CLI that reads its prompt and writes ``VERDICT`` to the file named by ``-o``."""
    path.write_text(
        "#!/bin/sh\ncat > /dev/null\n"
        'while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done\n'
        f"echo '{json.dumps(VERDICT)}' > \"$out\"\n"
    )
    return str(path)


def test_a_rerun_retries_a_failed_judgement_and_the_decision_waits_for_it(tmp_path):
    """A failed judgement must not be treated as done, or the decision would rest on fewer items than it reports."""
    cases = planted_cases(4)
    run = tmp_path / "run"
    planted_answers(run, [c["case_id"] for c in cases], unanswered={})
    judge_run = run / "judge-codex"
    planted_judge.items(cases, run, judge_run, ["E", "P"])
    cli = tmp_path / "codex"
    spec = JudgeSpec.of("codex", executable=stub_cli(cli, exit_code=1))

    planted_judge.judge(judge_run, spec, 0.0, 1)
    report = planted_judge.score(judge_run, [("E", "P")], heldout=True)
    assert report["decision"]["status"] == "incomplete"
    assert len(report["failed_judgements"]) == 8, "every item of both arms failed"

    verdict_cli(cli)  # the same executable now answers
    planted_judge.judge(judge_run, spec, 0.0, 1)
    report = planted_judge.score(judge_run, [("E", "P")], heldout=True)
    assert report["failed_judgements"] == []
    assert report["decision"]["status"] == "complete"


def write_arm(run: Path, arm: str, body: str, *, status: str = "active") -> None:
    page = "Knowledge Base/Notes/Insights/rule.md"
    path = run / arm / "built" / "vault" / page
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nexomem_id: {arm}-id\nstatus: {status}\n---\n{body}")
    (run / arm / "authored.json").write_text(json.dumps([page]))
    governance = {"inserted": "- verdict: refuted\n"} if arm == "E" else None
    pages = [{"stale_path": page, "correction_path": page, "governance": governance}]
    (run / arm / "pages.json").write_text(json.dumps(pages))


def test_arm_e_may_differ_from_arm_p_only_by_its_own_governance_lines(tmp_path):
    """If the authored text drifts between arms, the governance is no longer the only difference."""
    write_arm(tmp_path, "P", "- id: u1\n\nThe rule is 7 days.\n")
    write_arm(tmp_path, "E", "- id: u1\n- verdict: refuted\n\nThe rule is 7 days.\n")
    planted_build.require_identical_text(tmp_path)

    shutil.rmtree(tmp_path / "E")
    write_arm(tmp_path, "E", "- id: u1\n- verdict: refuted\n\nThe rule is 14 days.\n")
    with pytest.raises(RuntimeError, match="differ beyond the builder's governance"):
        planted_build.require_identical_text(tmp_path)


def rejecting_cli(path: Path, launches: Path) -> Path:
    """A CLI that starts a session, then reports the subscription limit as rejected and ends in an error result."""
    init = {
        "type": "system",
        "subtype": "init",
        "model": "m",
        "apiKeySource": "none",
        "mcp_servers": [{"name": "exomem", "status": "connected"}],
    }
    limit = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "resetsAt": 1}}
    error = {"type": "result", "subtype": "error", "is_error": True, "result": "limit reached"}
    lines = "\n".join(f"echo '{json.dumps(event)}'" for event in (init, limit, error))
    path.write_text(f"#!/bin/sh\necho launched >> {launches}\nread line\n{lines}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_a_rejected_limit_stops_the_run_and_the_decision_waits_for_room(tmp_path, monkeypatch):
    """Quota that ran out must not score the arm that ran last as a loss, nor be retried or go on to the next case."""
    launches = tmp_path / "launches"
    cli = rejecting_cli(tmp_path / "claude", launches)
    envelope = AgentEnvelope(executable=cli, version="0.0-test")
    monkeypatch.setattr(planted_answer, "discover_agent_envelope", lambda: envelope)
    cases = planted_cases(4)
    case_ids = [c["case_id"] for c in cases]
    run = tmp_path / "run"
    (run / "E" / "built" / "vault").mkdir(parents=True)
    (run / "E" / "built" / "exomem-state").mkdir()

    with pytest.raises(LimitRejected):
        answer_all(cases, run / "E", "m", "high")

    assert not list((run / "E" / "answers").glob("*.json")), "a rejected limit is no answer"
    assert launches.read_text().count("launched") == 1, "no retry, and no later case"

    # Arm P ran before the limit ran out. Arm E has no answers, so the decision decides nothing yet.
    planted_answers(run, case_ids, unanswered={}, arms=("P",))
    judge_run = run / "judge-codex"
    planted_judge.items(cases, run, judge_run, ["E", "P"])
    shown = (judge_run / "judge" / "items.jsonl").read_text().splitlines()
    (judge_run / "judge" / "verdicts.jsonl").write_text(
        "".join(
            json.dumps({"id": json.loads(line)["id"], "verdict": VERDICT}) + "\n" for line in shown
        )
    )
    (judge_run / "judge" / "judge.json").write_text(json.dumps({"backend": "codex", "model": "m"}))
    decision = planted_judge.score(judge_run, [("E", "P")], heldout=True)["decision"]
    assert decision["status"] == "incomplete"
    assert any("E" in reason for reason in decision["why"])


def test_a_rerun_replays_only_the_matter_a_stop_left_unfinished(tmp_path, monkeypatch):
    """A half-played matter replays whole from a clean folder, and a finished one is never played again."""
    played = []

    def run_arm(lab, case, prepared_task, arm, copy_to):
        played.append((case["task_id"], arm))
        folder = lab.run_dir / slug(case["task_id"]) / arm
        folder.mkdir(parents=True)
        (folder / "result.json").write_text("{}")

    monkeypatch.setattr(lab_run, "run_arm", run_arm)
    run, prepared = tmp_path / "run", tmp_path / "prepared"
    for task in ("finished", "stopped"):
        (prepared / "tasks" / slug(task)).mkdir(parents=True)
        (prepared / "tasks" / slug(task) / "case.json").write_text(json.dumps({"task_id": task}))
    for arm in ("S", "WP"):
        (run / slug("finished") / arm).mkdir(parents=True)
        (run / slug("finished") / arm / "result.json").write_text("{}")
    (run / slug("stopped") / "S").mkdir(parents=True)
    (run / slug("stopped") / "S" / "result.json").write_text("{}")
    (run / slug("stopped") / "WP").mkdir()  # the arm the limit stopped: no result.json

    for task in ("finished", "stopped"):
        lab_run.run_task(SimpleNamespace(run_dir=run), prepared, task, ["S", "WP"])

    assert played == [("stopped", "S"), ("stopped", "WP")]
    assert (run / "unfinished" / f"{slug('stopped')}-0" / "WP").is_dir()


def test_a_resume_with_another_product_revision_or_cli_is_refused_before_any_session(
    tmp_path, monkeypatch
):
    """Units that two calls play must come from one product and one CLI, or the report mixes them."""
    run = tmp_path / "run"
    first = {
        "suite": "planted",
        "split": "dev",
        "model": "m",
        "effort": "high",
        "limit": None,
        "arms": ["E"],
        "seeds": None,
        "product_revision": "a1",
        "product_dirty": False,
        "cli": "2.0",
    }
    write_record(run, first)
    write_record(run, {**first, "arms": ["P"]})  # the same product and CLI: a resume adds the arm
    for field, value in (("product_revision", "b2"), ("cli", "2.1"), ("product_dirty", True)):
        with pytest.raises(SystemExit, match=f"records {field}.*use a new --run-id"):
            write_record(run, {**first, field: value})

    # The planted arm manifest is written once, and a resume with another CLI stops before its first session.
    launches = tmp_path / "launches"
    cli = rejecting_cli(tmp_path / "claude", launches)
    monkeypatch.setattr(
        planted_answer, "discover_agent_envelope", lambda: AgentEnvelope(cli, "2.0")
    )
    arm = tmp_path / "arm" / "E"
    (arm / "built" / "vault").mkdir(parents=True)
    (arm / "built" / "exomem-state").mkdir()
    with pytest.raises(LimitRejected):
        answer_all(planted_cases(2), arm, "m", "high")
    monkeypatch.setattr(
        planted_answer, "discover_agent_envelope", lambda: AgentEnvelope(cli, "2.1")
    )
    with pytest.raises(SystemExit, match="records cli"):
        answer_all(planted_cases(2), arm, "m", "high")
    assert launches.read_text().count("launched") == 1
