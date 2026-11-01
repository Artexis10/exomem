"""Run scenarios through a real headless agent, or dry-run them without one.

A scored run takes a suite (`scenarios/` for `import`, `scenarios/<suite>/` for any other) and, for a comparison, an
arm: a checkout whose tool text, skill and recorded replies the agent is shown (`recording.py`).

Reused by import: the isolation floor, the unsafe-cwd refusal, the CLI discovery and the run-fault reasons from
`epistemic.journeys.f27_replay`, and the stream-json reader from `membench.trackc.witness_join`.
Their `build_turn_argv` is not reused: it takes an f27 `Arm` and a seeded vault this probe has not.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from epistemic.journeys.f27_replay import (
    REPO_ROOT,
    discover_agent_envelope,
    environment_floor,
    fault_reason,
    refuse_unsafe_out_dir,
)
from membench.trackc.witness_join import parse_stream_json_transcript

from . import checks, judge, recording
from .recording import Arm
from .scripted_replies import (
    DEFAULT_SUITE,
    SCENARIO_DIR,
    Scenario,
    load_scenario,
    scenario_dir,
    tool_specs,
)

#: The skill files an agent is shown, relative to a checkout's `_Schema`. The import suite adds the importing reference.
SKILL_FILES = {
    DEFAULT_SUITE: ("SKILL.md", "references/importing.md"),
    "write-narration": ("SKILL.md",),
}
SERVER_SCRIPT = Path(__file__).resolve().parent / "server.py"
MAX_TURNS = 20
RUN_TIMEOUT_SECONDS = 600.0
JUDGE_TIMEOUT_SECONDS = 300.0

Runner = Callable[[list[str], Path, Mapping[str, str], float], subprocess.CompletedProcess]


def load_scenarios(selector: str, suite: str = DEFAULT_SUITE) -> list[Scenario]:
    paths = sorted(scenario_dir(suite).glob("*.json"))
    if not paths:
        raise SystemExit(f"no scenarios in suite {suite!r}")
    scenarios = [load_scenario(p) for p in paths]
    if selector == "all":
        return scenarios
    chosen = [s for s in scenarios if s.id == selector or s.id.startswith(selector)]
    if not chosen:
        raise SystemExit(f"no scenario matches {selector!r}: {[s.id for s in scenarios]}")
    return chosen


def skill_text(root: Path = REPO_ROOT, suite: str = DEFAULT_SUITE) -> str:
    """The skill as a user has it from one checkout: SKILL.md, then the suite's reference files."""
    schema = root / "src" / "exomem" / "_scaffold" / "_Schema"
    return "\n\n".join((schema / name).read_text(encoding="utf-8") for name in SKILL_FILES[suite])


def mcp_config(
    path: Path, scenario_path: Path, call_log: Path, *, extra_env: Mapping[str, str] | None = None
) -> Path:
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    checks.SERVER: {
                        "command": sys.executable,
                        "args": [str(SERVER_SCRIPT)],
                        "env": {
                            "IG_SCENARIO": str(scenario_path),
                            "IG_CALL_LOG": str(call_log),
                            "FASTMCP_LOG_LEVEL": "ERROR",
                            **(extra_env or {}),
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def agent_argv(
    executable: Path, scenario: Scenario, *, mcp: Path, skill: Path, model: str
) -> list[str]:
    allowed = [f"mcp__{checks.SERVER}__{name}" for name in scenario.served_tools] + list(
        scenario.builtin_tools
    )
    return [
        str(executable),
        "-p",
        scenario.prompt,
        "--strict-mcp-config",
        "--mcp-config",
        str(mcp),
        "--setting-sources",
        "project",
        "--output-format",
        "stream-json",
        "--verbose",
        "--allowedTools",
        ",".join(allowed),
        "--tools",
        ",".join(scenario.builtin_tools),
        "--max-turns",
        str(MAX_TURNS),
        "--model",
        model,
        "--no-session-persistence",
        "--append-system-prompt-file",
        str(skill),
    ]


def judge_argv(
    executable: Path, prompt: str, schema: dict, *, empty_mcp: Path, model: str
) -> list[str]:
    return [
        str(executable),
        "-p",
        prompt,
        "--strict-mcp-config",
        "--mcp-config",
        str(empty_mcp),
        "--setting-sources",
        "project",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(schema),
        "--tools",
        "",
        "--max-turns",
        "3",
        "--model",
        model,
        "--no-session-persistence",
    ]


def _subprocess_runner(
    argv: list[str], cwd: Path, env: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=str(cwd), env=dict(env), capture_output=True, text=True, timeout=timeout
    )  # noqa: S603


def install_fake_commands(scenario: Scenario, bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name, text in scenario.fake_commands.items():
        target = bin_dir / name
        target.write_text(text, encoding="utf-8")
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def read_call_log(path: Path) -> tuple[dict, ...]:
    if not path.exists():
        return ()
    return tuple(
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    )


def read_shell_events(path: Path) -> dict[str, float]:
    """The fake command writes `start <epoch>` and `exit <epoch>` lines."""
    events: dict[str, float] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, _, stamp = line.partition(" ")
            events[name] = float(stamp)
    return events


def child_env(parent: Mapping[str, str], bin_dir: Path, shell_log: Path) -> dict[str, str]:
    """The f27 floor (session markers dropped, HOME kept) without `EXOMEM_*`, the fake commands first on PATH.

    f27's `arm_environment` drops `EXOMEM_*` for the same reason: the agent's shell must not reach a real vault.
    """
    floor, _removed = environment_floor(parent)
    floor = {key: value for key, value in floor.items() if not key.startswith("EXOMEM_")}
    floor["PATH"] = f"{bin_dir}{os.pathsep}{floor.get('PATH', '')}"
    floor["IG_SHELL_LOG"] = str(shell_log)
    return floor


def server_fault(stdout: str) -> str | None:
    """Why the scripted server cannot have served this run, or None. As `f33_referent_replay.session_fault` does:
    a run counts only when every `init` event shows the server `connected`. A failed server gives the agent no tool,
    and a scenario that the skill text alone answers would then pass without ever seeing the scripted states."""
    inits = []
    for raw in stdout.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if (
            isinstance(event, dict)
            and event.get("type") == "system"
            and event.get("subtype") == "init"
        ):
            inits.append(event)
    if not inits:
        return "the stream has no init event, so the scripted server's connection is unknown"
    for init in inits:
        servers = {
            s.get("name"): s.get("status")
            for s in init.get("mcp_servers") or ()
            if isinstance(s, dict)
        }
        if servers.get(checks.SERVER) != "connected":
            return f"the {checks.SERVER} server is not connected: {servers.get(checks.SERVER)!r}"
    return None


def text_under_test(scenario: Scenario, root: Path = REPO_ROOT) -> dict[str, str]:
    """Digests of what the agent was shown, so a row ties to a revision of the skill and the tool text."""
    served = {name: tool_specs(root)[name] for name in scenario.served_tools}
    return {
        "skill_sha256": hashlib.sha256(
            skill_text(root, scenario.suite).encode("utf-8")
        ).hexdigest(),
        "tool_schemas_sha256": hashlib.sha256(
            json.dumps(served, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }


def repo_sha() -> str:
    """The checkout's commit, or `unknown` when git cannot say."""
    done = subprocess.run(  # noqa: S603,S607
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else "unknown"


def init_field(stdout: str, name: str) -> object:
    """A field of the stream's first `init` event (`apiKeySource` tells a subscription login from an API key)."""
    for raw in stdout.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("subtype") == "init":
            return event.get(name)
    return None


def run_once(
    scenario: Scenario,
    *,
    repeat: int,
    model: str,
    judge_model: str,
    out: Path,
    executable: Path,
    stamp: Mapping[str, str] | None = None,
    run: Runner = _subprocess_runner,
    arm: Arm | None = None,
    recorded: Path | None = None,
) -> dict:
    """One run. `stamp` adds fields to the row (`run_all` gives the repo SHA and the CLI version).
    `arm` and `recorded` (that arm's recording of this scenario) show the agent the arm's own tool text,
    skill, server instructions and replies."""
    root = arm.root if arm else REPO_ROOT
    scenario_path = scenario.path or SCENARIO_DIR / f"{scenario.id}.json"
    recording_data = recording.load_recording(recorded) if recorded else {}
    shapes = recording_data.get("shapes")
    work = out / "work"
    if arm:
        work = work / model / arm.label
    work = work / f"{scenario.id}-{repeat}"
    work.mkdir(parents=True, exist_ok=True)
    call_log, shell_log = work / "calls.jsonl", work / "shell.log"
    stream_path = work / "stream.jsonl"
    # The server and the fake command append, so a log from an earlier run into the same folder would read as this
    # run's. The agent's folder starts empty for the same reason.
    for stale in (call_log, shell_log, stream_path):
        stale.unlink(missing_ok=True)
    cwd = work / "cwd"
    shutil.rmtree(cwd, ignore_errors=True)
    cwd.mkdir()
    skill = work / "skill.md"
    skill.write_text(skill_text(root, scenario.suite), encoding="utf-8")
    mcp = mcp_config(
        work / "mcp.json",
        scenario_path,
        call_log,
        extra_env=(
            {"IG_PRODUCT_ROOT": str(root), **({"IG_SHAPES": str(recorded)} if recorded else {})}
            if arm
            else None
        ),
    )
    empty_mcp = work / "empty-mcp.json"
    empty_mcp.write_text('{"mcpServers": {}}', encoding="utf-8")
    bin_dir = work / "bin"
    install_fake_commands(scenario, bin_dir)
    if scenario.source_folder:
        folder = cwd / scenario.source_folder
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "notes.md").write_text("A note in the folder to import.\n", encoding="utf-8")
    env = child_env(os.environ, bin_dir, shell_log)

    row: dict = {
        "scenario": scenario.id,
        "repeat": repeat,
        "model": model,
        "judge_model": judge_model,
        **({"arm": arm.label, "product_sha": recording_data.get("product_sha")} if arm else {}),
        **text_under_test(scenario, root),
        **(stamp or {}),
        "error": None,
    }
    try:
        proc = run(
            agent_argv(executable, scenario, mcp=mcp, skill=skill, model=model),
            cwd,
            env,
            RUN_TIMEOUT_SECONDS,
        )
        stream_path.write_text(proc.stdout, encoding="utf-8")
        transcript = parse_stream_json_transcript(proc.stdout.splitlines())
        row["error"] = (
            fault_reason(proc, transcript)
            or server_fault(proc.stdout)
            or (None if transcript.result_text else "the agent returned no text")
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        row["error"] = f"{type(error).__name__}: {error}"
        transcript = parse_stream_json_transcript(())
        proc = None

    evidence = checks.Evidence(
        calls=read_call_log(call_log),
        tool_names=tuple(t.name for t in transcript.tool_uses),
        shell_events=read_shell_events(shell_log),
        answer=transcript.result_text,
        shapes=shapes,
    )
    deterministic = checks.run_checks(
        scenario.checks, evidence, scenario.served_tools, scenario.builtin_tools
    )
    answer = None
    if row["error"] is None and scenario.criteria:
        answer_view = scenario.judge_view == "answer"
        rendered = (
            judge.render_answer_view(
                scenario.prompt, fill_reference(scenario.reference, shapes), transcript.result_text
            )
            if answer_view
            else judge.render_transcript(scenario.prompt, proc.stdout.splitlines())
        )
        shown = tuple({**c, "text": fill_reference(c["text"], shapes)} for c in scenario.criteria)
        schema = judge.answer_schema(shown)
        try:
            judged = run(
                judge_argv(
                    executable,
                    judge.judge_prompt(rendered, shown, answer_view=answer_view),
                    schema,
                    empty_mcp=empty_mcp,
                    model=judge_model,
                ),
                cwd,
                env,
                JUDGE_TIMEOUT_SECONDS,
            )
            answer = judge.parse_answer(judged.stdout, scenario.criteria)
        except (subprocess.TimeoutExpired, OSError):
            answer = None
    judged_outcomes = checks.judged_outcomes(scenario.criteria, answer)
    row.update(
        verdict=checks.verdict(row["error"], deterministic, judged_outcomes),
        checks=[o.__dict__ for o in deterministic],
        criteria=[o.__dict__ for o in judged_outcomes],
        calls=[{"tool": c["tool"], "arguments": c["arguments"]} for c in evidence.calls],
        final_text=transcript.result_text,
        cost_usd=transcript.total_cost_usd,
    )
    if proc is not None:
        row["api_key_source"] = init_field(proc.stdout, "apiKeySource")
        # Every message the person could have read, not only the last: a leak in a message before the answer.
        said = judge.assistant_texts(proc.stdout.splitlines())
        row["said"] = said
        row["said_leaks"] = sorted(
            {
                leak
                for spec in scenario.checks
                if spec["type"] == "answer_lacks_scripted_strings" and shapes
                for leak in checks.leaked_strings("\n".join(said), shapes, spec)
            }
        )
    return row


def fill_reference(reference: str, shapes: Mapping[str, object] | None) -> str:
    """Put a scripted reply's field into a reference or criterion, so the judge sees the id the person asked for.
    `{shape.key}` names one field and the value is the first such field in the reply. The placeholder is this
    harness's own template syntax, a closed format, not a reading of anyone's words."""
    for token in dict.fromkeys(re.findall(r"\{(\w+)\.(\w+)\}", reference)):
        name, key = token
        values = list(checks.values_under((shapes or {}).get(name), (key,)))
        if values:
            reference = reference.replace("{" + name + "." + key + "}", values[0])
    return reference


def summary_table(rows: list[dict]) -> str:
    """Pass, fail and unknown counts per scenario, and per arm and model when the rows carry them."""
    split = any("arm" in r for r in rows)
    lines = [
        "| " + ("arm | model | " if split else "") + "scenario | pass | fail | unknown |",
        "|" + "---|" * (6 if split else 4),
    ]
    groups = dict.fromkeys((r.get("arm"), r["model"], r["scenario"]) for r in rows)
    for arm, model, sid in groups:
        mine = [
            r["verdict"]
            for r in rows
            if (r.get("arm"), r["model"], r["scenario"]) == (arm, model, sid)
        ]
        lead = f"{arm} | {model} | " if split else ""
        lines.append(
            f"| {lead}{sid} | {mine.count('pass')} | {mine.count('fail')} | {mine.count('unknown')} |"
        )
    return "\n".join(lines)


def run_all(
    scenarios: list[Scenario],
    *,
    model: str,
    judge_model: str,
    repeats: int,
    out: Path,
    arms: Sequence[Arm] = (),
) -> list[dict]:
    """Every scenario `repeats` times. With `arms`, each repeat runs the arms one after the other, so a slow
    hour of the provider falls on both. Each arm is recorded first, once per out folder."""
    out = refuse_unsafe_out_dir(out)
    out.mkdir(parents=True, exist_ok=True)
    envelope = discover_agent_envelope()
    executable = envelope.executable
    stamp = {"repo_sha": repo_sha(), "cli_version": envelope.version}
    recorded: dict[tuple[str, str], Path] = {}
    for arm in arms:
        for scenario in scenarios:
            if scenario.record:
                target = out / "shapes" / arm.label / f"{scenario.id}.json"
                problems = recording.record(scenario, arm, target, out / "scratch" / arm.label)
                if problems:
                    raise SystemExit("; ".join(problems))
                recorded[(arm.label, scenario.id)] = target
    shutil.rmtree(out / "scratch", ignore_errors=True)
    rows = []
    with (out / "runs.jsonl").open("a", encoding="utf-8") as sink:
        for scenario in scenarios:
            for repeat in range(repeats):
                for arm in arms or (None,):
                    row = run_once(
                        scenario,
                        repeat=repeat,
                        model=model,
                        judge_model=judge_model,
                        out=out,
                        executable=executable,
                        stamp=stamp,
                        arm=arm,
                        recorded=recorded.get((arm.label, scenario.id)) if arm else None,
                    )
                    sink.write(json.dumps(row) + "\n")
                    sink.flush()
                    rows.append(row)
                    print(
                        f"{model} {arm.label if arm else '-'} {scenario.id}#{repeat}: {row['verdict']}",
                        file=sys.stderr,
                        flush=True,
                    )
    summary = f"summary-{model}.md" if arms else "summary.md"
    (out / summary).write_text(summary_table(rows) + "\n", encoding="utf-8")
    return rows


# --------------------------------------------------------------------------
# Dry run: no model.
# --------------------------------------------------------------------------


async def _list_and_call(
    scenario: Scenario, scenario_path: Path, call_log: Path, env_extra: Mapping[str, str]
) -> tuple[dict, object]:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
        env={
            "IG_SCENARIO": str(scenario_path),
            "IG_CALL_LOG": str(call_log),
            "FASTMCP_LOG_LEVEL": "ERROR",
            **env_extra,
        },
    )
    async with Client(transport) as client:
        listed = {t.name: t for t in await client.list_tools()}
        first = scenario.served_tools[0]
        reply = await client.call_tool(first, {}, raise_on_error=False)
    return listed, reply


def dry_run(scenario: Scenario, scratch: Path, arm: Arm | None = None) -> list[str]:
    """Problems found; empty when the scenario file, the served tool list and the logging all hold.
    With an `arm` it also makes the scenario's real calls on that checkout (`recording.py`) and checks that every
    named reply was recorded, every answer check has something to look for, and the server shows the arm's tool text."""
    problems: list[str] = []
    root = arm.root if arm else REPO_ROOT
    specs = tool_specs(root)
    scenario_path = scenario.path or SCENARIO_DIR / f"{scenario.id}.json"
    call_log = scratch / f"{scenario.id}.{arm.label if arm else 'current'}.calls.jsonl"
    for tool_name in scenario.served_tools:
        if tool_name not in specs:
            problems.append(f"serves {tool_name}, which has no schema")
    for key in scenario.replies:
        if key.partition(".")[0] not in scenario.served_tools:
            problems.append(f"reply key {key} names a tool the scenario does not serve")
    for spec in scenario.checks:
        if spec.get("type") not in checks.CHECKS:
            problems.append(f"unknown check type {spec.get('type')}")
    for criterion in scenario.criteria:
        if not {"id", "text", "expect"} <= set(criterion) or not isinstance(
            criterion["expect"], bool
        ):
            problems.append(f"criterion {criterion.get('id')} needs id, text and a boolean expect")
    for name, text in scenario.fake_commands.items():
        syntax = subprocess.run(["sh", "-n"], input=text, text=True, capture_output=True)  # noqa: S603,S607
        if syntax.returncode != 0:
            problems.append(f"fake command {name}: {syntax.stderr.strip()}")
    if problems:
        return problems
    env_extra: dict[str, str] = {}
    shapes = None
    if arm:
        env_extra["IG_PRODUCT_ROOT"] = str(root)
    if arm and scenario.record:
        target = scratch / f"{scenario.id}.{arm.label}.shapes.json"
        problems = recording.record(scenario, arm, target, scratch / f"record-{arm.label}")
        if problems:
            return problems
        recorded = recording.load_recording(target)
        shapes = recorded["shapes"]
        env_extra["IG_SHAPES"] = str(target)
        try:
            load_scenario(scenario_path, shapes)
        except SystemExit as error:
            return [str(error)]
        problems.extend(recording_problems(scenario, shapes))
    listed, reply = asyncio.run(_list_and_call(scenario, scenario_path, call_log, env_extra))
    if set(listed) != set(scenario.served_tools):
        problems.append(
            f"server lists {sorted(listed)}, scenario serves {sorted(scenario.served_tools)}"
        )
    for name, tool in listed.items():
        if (
            tool.description != specs[name]["description"]
            or tool.input_schema != specs[name]["inputSchema"]
        ):
            problems.append(f"{name}: description or schema differs from the fixture")
    if len(read_call_log(call_log)) != 1:
        problems.append("the probe call was not logged")
    return problems


def recording_problems(scenario: Scenario, shapes: Mapping[str, object]) -> list[str]:
    """Answer checks that would pass or fail for no reason: nothing to look for, or a placeholder left unfilled."""
    problems = []
    for spec in scenario.checks:
        if spec["type"] == "answer_lacks_scripted_strings":
            missing = [name for name in spec.get("shapes", ()) if name not in shapes]
            if missing:
                problems.append(f"check names shapes {missing}, which were not recorded")
            elif not checks.scripted_strings(shapes, spec):
                problems.append(
                    "answer_lacks_scripted_strings finds no scripted string to look for"
                )
        if spec["type"] == "answer_has_scripted_string":
            outcome = checks.answer_has_scripted_string(
                spec, checks.Evidence((), (), {}, answer="", shapes=shapes)
            )
            if outcome.verdict == checks.UNKNOWN:
                problems.append(outcome.detail)
    texts = [scenario.reference, *(c["text"] for c in scenario.criteria)]
    leftover = re.findall(r"\{\w+\.\w+\}", "\n".join(fill_reference(t, shapes) for t in texts))
    if leftover:
        problems.append(f"the reference keeps placeholders {leftover}")
    return problems
