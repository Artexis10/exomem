"""Answer each planted-error case with a fresh agent that can only read its Exomem memory.

The agent gets the case's task and Exomem's three read tools (``recall_context``,
``search_memory``, ``read_memory``) over one arm's built vault, and nothing else:
no built-in tools, no other MCP server. The prompt is the same in both arms, so
the vault's governance is the only difference. Every case answers from a fresh copy
of the arm's built vault and state (``built``), restored before each attempt, so no
read from one case reaches the next. A session that shows a harness fault is retried
with the same inputs; one that still faults leaves the case unanswered by the harness
and strikes it. ``membench outcome run planted`` drives it.
"""

from __future__ import annotations

import json
from pathlib import Path

from epistemic.journeys.f27_replay import (
    AgentEnvelope,
    Arm,
    build_turn_argv,
    discover_agent_envelope,
    refuse_unsafe_out_dir,
)

from .. import record
from ..clbench.run import QuestionLog, single_turn
from ..isolation import claude_env, copy_state, mcp_server
from ..workspace import sha256_of

READ_TOOLS = ("recall_context", "search_memory", "read_memory")
TIMEOUT_SECONDS = 300.0

PROMPT = (
    "You are the assistant for {domain}. Your memory (the Exomem tools) holds the notes "
    "from earlier work. Use it to find the facts you need, then complete this task.\n\n"
    "Task: {task}"
)


def answer_actor(
    arm_dir: Path, model: str, effort: str, envelope: AgentEnvelope
) -> tuple[list[str], dict[str, str], Path]:
    """The answering agent's argv, environment and MCP config: read tools over this arm's vault only."""
    mcp_config = arm_dir / "answer-mcp.json"
    mcp_config.write_text(json.dumps({"mcpServers": {"exomem": mcp_server(arm_dir)}}, indent=2))
    arm = Arm(
        arm_id=arm_dir.name,
        prominence="instruction",
        tools="",
        allowed_tools=",".join(f"mcp__exomem__{t}" for t in READ_TOOLS),
        uses_plugin=False,
        uses_custom_instructions=False,
        configure_ref="membench.outcome.planted",
    )
    argv = build_turn_argv(
        executable=envelope.executable,
        arm=arm,
        prompt=None,
        mcp_config=mcp_config,
        session_id=None,
        model=model,
        max_turns=None,
        first=True,
        append_system_prompt_file=None,
        plugin_dir=None,
    ) + ["--effort", effort]
    return argv, claude_env()[0], mcp_config


def answer_all(cases: list[dict], arm_dir: Path, model: str, effort: str) -> Path:
    arm_dir = refuse_unsafe_out_dir(arm_dir)
    built = arm_dir / "built"
    if not built.is_dir():
        raise SystemExit(f"{arm_dir} has no built vault to answer from; build it first")
    out = arm_dir / "answers"
    out.mkdir(exist_ok=True)
    envelope = discover_agent_envelope()
    argv, env, mcp_config = answer_actor(arm_dir, model, effort, envelope)
    removed = claude_env()[1]
    mcp_config_sha256 = sha256_of(mcp_config)
    manifest_file = arm_dir / "manifest.json"
    manifest = {
        "arm": arm_dir.name,
        "actor": {
            **record.actor_record(cli=envelope.version, model=model, effort=effort, root=arm_dir),
            "mcp_config_sha256": mcp_config_sha256,
            "argv": argv,
            "env_removed": list(removed),
        },
    }
    manifest_file.write_text(json.dumps(manifest, indent=2))
    work = arm_dir / "answer-cwd"
    work.mkdir(exist_ok=True)
    for case in cases:
        target = out / f"{case['case_id']}.json"
        if target.exists():
            continue
        log = QuestionLog(question_id=case["case_id"], index=0)
        text = single_turn(
            argv,
            env,
            work,
            out / f"{case['case_id']}.jsonl",
            PROMPT.format(domain=case["domain"], task=case["task"]),
            log,
            model=model,
            exomem=True,
            timeout=TIMEOUT_SECONDS,
            # Every attempt, the first included, starts from the vault and state as built.
            reset=lambda _number: copy_state(built, arm_dir),
        )
        answered = {
            "case_id": case["case_id"],
            "answer": text,
            # No answer and no harness fault: the actor timed out. A product failure, scored as unanswered.
            "timed_out": text is None and not log.harness_faults,
            "log": log.__dict__,
            "cli": envelope.version,
            "model_requested": model,
            "effort": effort,
            "mcp_config_sha256": mcp_config_sha256,
        }
        target.write_text(json.dumps(answered, indent=2))
        if "observed" not in manifest["actor"] and log.init:
            manifest["actor"]["observed"] = record.observed(log.init)
            manifest_file.write_text(json.dumps(manifest, indent=2))
        print(
            f"{case['case_id']} tools={log.tool_uses} faults={len(log.faults)} "
            f"harness={len(log.harness_faults)}",
            flush=True,
        )
    return out
