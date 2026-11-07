"""The outcome evaluation harness measures what it says it measures.

Three failures a green run would not reveal: an actor that reaches the live vault, a judge
that can tell which arm wrote an answer, and a scorer that disagrees with the upstream key.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from epistemic.journeys.f27_replay import PLUGIN_DIR, AgentEnvelope
from membench.outcome.clbench.run import ARMS, CellSpec, ClaudeActor, run_cell
from membench.outcome.clbench.run import UPSTREAM as CLBENCH_UPSTREAM
from membench.outcome.isolation import child_env, mcp_server, server_pins
from membench.outcome.judging import Judge, JudgeSpec
from membench.outcome.lab import judge as lab_judge
from membench.outcome.lab import run as lab_run
from membench.outcome.lab.prepare import CASES, slug
from membench.outcome.lab.prepare import UPSTREAM as LAB_UPSTREAM
from membench.outcome.lab.run import FILE_ALLOWS, SHIPPED_ALLOWS, Lab
from membench.outcome.planted import judge as planted_judge
from membench.outcome.planted.answer import answer_actor
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
    argv, env, mcp_config = answer_actor(answer_dir, "model-under-test", "high", None, ENVELOPE)
    server = json.loads(mcp_config.read_text())["mcpServers"]["exomem"]["env"]
    surfaces["planted E"] = (answer_dir, [env], [server], argv, mcp_config)
    pins = tmp_path / "pins"
    surfaces["pins"] = (pins, [child_env(pins)], [server_pins(pins)], [], None)
    judge = Judge(JudgeSpec.of("claude", executable=str(ENVELOPE.executable)), tmp_path / "judge")
    surfaces["judge"] = (
        tmp_path / "judge",
        [judge.env],
        [],
        judge.argv("prompt", {}, tmp_path / "out"),
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
        reads = [rule for rule in allowed if rule.startswith("Read(")]
        assert reads == ["Read(./**)", f"Read(/{PLUGIN_DIR}/**)"], f"{name} reads {reads}"
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
                json.dumps({"faults": [], "deliverable": {"file": "deliverable.md"}})
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
                json.dumps({"answer": f"Use 14 days for {case['case_id']}."})
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
