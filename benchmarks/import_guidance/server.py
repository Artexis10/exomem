"""Scripted stdio MCP server: real tool names, descriptions and schemas, replies
from a scenario file. Env: IG_SCENARIO (scenario path), IG_CALL_LOG (JSONL)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import scripted_replies as sr  # noqa: E402
from fastmcp import FastMCP  # noqa: E402
from fastmcp.tools import Tool, ToolResult  # noqa: E402


class ScriptedTool(Tool):
    """A tool whose reply comes from the scenario, and whose call is logged."""

    scenario: sr.Scenario
    call_log: Path
    counts: dict

    async def run(self, arguments: dict) -> ToolResult:
        kind = sr.call_kind(arguments)
        count_key = f"{self.name}.{kind}"
        prior = self.counts.get(count_key, 0)
        self.counts[count_key] = prior + 1
        reply = sr.pick_reply(self.scenario, self.name, arguments, prior)
        with self.call_log.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"t": time.time(), "tool": self.name, "kind": kind, "arguments": arguments}
                )
                + "\n"
            )
        return ToolResult(content=json.dumps(reply, indent=2))


def build_server(scenario: sr.Scenario, call_log: Path) -> FastMCP:
    specs = sr.tool_specs()
    missing = [name for name in scenario.served_tools if name not in specs]
    if missing:
        raise SystemExit(f"scenario {scenario.id} serves tools with no schema: {missing}")
    server = FastMCP("exomem")
    counts: dict = {}
    for name in scenario.served_tools:
        spec = specs[name]
        server.add_tool(
            ScriptedTool(
                name=name,
                description=spec["description"],
                parameters=spec["inputSchema"],
                scenario=scenario,
                call_log=call_log,
                counts=counts,
            )
        )
    return server


if __name__ == "__main__":
    build_server(
        sr.load_scenario(Path(os.environ["IG_SCENARIO"])), Path(os.environ["IG_CALL_LOG"])
    ).run(transport="stdio", show_banner=False)
