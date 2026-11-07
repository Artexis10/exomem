"""Answer each planted-error case with a fresh agent that can only read its Exomem memory.

The agent gets the case's task and Exomem's three read tools (``recall_context``,
``search_memory``, ``read_memory``) over one arm's built vault, and nothing else:
no built-in tools, no other MCP server. The prompt is the same in both arms, so
the vault's governance is the only difference. Reads leave the vault as built,
so every case meets the same memory. ``membench outcome run planted`` drives it.
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
from ..clbench.run import ClaudeSession, QuestionLog
from ..isolation import claude_env, mcp_server

READ_TOOLS = ("recall_context", "search_memory", "read_memory")
TIMEOUT_SECONDS = 300.0

PROMPT = (
    "You are the assistant for {domain}. Your memory (the Exomem tools) holds the notes "
    "from earlier work. Use it to find the facts you need, then complete this task.\n\n"
    "Task: {task}"
)


def answer_actor(
    arm_dir: Path,
    model: str,
    effort: str,
    product_python: Path | None,
    envelope: AgentEnvelope,
) -> tuple[list[str], dict[str, str], Path]:
    """The answering agent's argv, environment and MCP config: read tools over this arm's vault only."""
    mcp_config = arm_dir / "answer-mcp.json"
    mcp_config.write_text(
        json.dumps({"mcpServers": {"exomem": mcp_server(arm_dir, product_python)}}, indent=2)
    )
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


def answer_all(
    cases: list[dict],
    arm_dir: Path,
    model: str,
    effort: str,
    product_python: Path | None = None,
) -> Path:
    arm_dir = refuse_unsafe_out_dir(arm_dir)
    out = arm_dir / "answers"
    out.mkdir(exist_ok=True)
    envelope = discover_agent_envelope()
    argv, env, mcp_config = answer_actor(arm_dir, model, effort, product_python, envelope)
    removed = claude_env()[1]
    manifest_file = arm_dir / "manifest.json"
    manifest = {
        "arm": arm_dir.name,
        "actor": {
            **record.actor_record(
                cli=envelope.version,
                model=model,
                effort=effort,
                mcp_config=mcp_config,
                root=arm_dir,
                product_python=product_python,
            ),
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
        session = ClaudeSession(argv, env, work, out / f"{case['case_id']}.jsonl")
        try:
            text = session.send(
                PROMPT.format(domain=case["domain"], task=case["task"]), log, TIMEOUT_SECONDS
            )
        finally:
            session.close()
        log.turns = 1
        answered = {
            "case_id": case["case_id"],
            "answer": text,
            "timed_out": text is None,
            "log": log.__dict__,
            "cli": envelope.version,
            "model_requested": model,
            "effort": effort,
        }
        target.write_text(json.dumps(answered, indent=2))
        if "observed" not in manifest["actor"]:
            manifest["actor"]["observed"] = record.observed(log.init)
            manifest_file.write_text(json.dumps(manifest, indent=2))
        print(f"{case['case_id']} tools={log.tool_uses} faults={len(log.faults)}", flush=True)
    return out
