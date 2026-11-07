"""Answer each planted-error case with a fresh agent that can only read its Exomem memory.

The agent gets the case's task and Exomem's three read tools (``recall_context``,
``search_memory``, ``read_memory``) over one arm's built vault, and nothing else:
no built-in tools, no other MCP server. The prompt is the same in both arms, so
the vault's governance is the only difference. Reads leave the vault as built,
so every case meets the same memory.

Usage (``PYTHONPATH=benchmarks:src``)::

    python -m membench.outcome.planted.answer --cases <dev.jsonl> --arm-dir <run>/E
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from epistemic.journeys.f27_replay import (
    Arm,
    build_turn_argv,
    discover_agent_envelope,
    environment_floor,
    refuse_unsafe_out_dir,
)

from ..clbench.run import ClaudeSession, QuestionLog
from ..isolation import mcp_server

READ_TOOLS = ("recall_context", "search_memory", "read_memory")
TIMEOUT_SECONDS = 300.0

PROMPT = (
    "You are the assistant for {domain}. Your memory (the Exomem tools) holds the notes "
    "from earlier work. Use it to find the facts you need, then complete this task.\n\n"
    "Task: {task}"
)


def answer_all(cases: list[dict], arm_dir: Path, model: str, effort: str) -> Path:
    arm_dir = refuse_unsafe_out_dir(arm_dir)
    out = arm_dir / "answers"
    out.mkdir(exist_ok=True)
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
    envelope = discover_agent_envelope()
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
    floor, _ = environment_floor(os.environ)
    env = {k: v for k, v in floor.items() if not k.startswith(("EXOMEM_", "KB_"))}
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
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
        record = {
            "case_id": case["case_id"],
            "answer": text,
            "timed_out": text is None,
            "log": log.__dict__,
            "cli": envelope.version,
            "model_requested": model,
            "effort": effort,
        }
        target.write_text(json.dumps(record, indent=2))
        print(f"{case['case_id']} tools={log.tool_uses} faults={len(log.faults)}", flush=True)
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--arm-dir", type=Path, required=True)
    parser.add_argument("--model", default="claude-sonnet-5-5")
    parser.add_argument("--effort", default="high")
    args = parser.parse_args(argv)
    cases = [json.loads(line) for line in args.cases.read_text().splitlines() if line.strip()]
    answer_all(cases, args.arm_dir, args.model, args.effort)


if __name__ == "__main__":
    main()
