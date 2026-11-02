"""Scripted stdio MCP server: real tool names, descriptions and schemas, replies
from a scenario file. Env: IG_SCENARIO (scenario path), IG_CALL_LOG (JSONL). Optional, for an
arm of a comparison: IG_PRODUCT_ROOT (the checkout whose tool text is served) and IG_SHAPES (that
arm's recorded replies and server instructions, from `recording.py`)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Started by path, outside the package, so put `benchmarks/` on the path to import `import_guidance`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastmcp import FastMCP  # noqa: E402
from fastmcp.tools import Tool, ToolResult  # noqa: E402

from import_guidance import scripted_replies as sr  # noqa: E402


class ScriptedTool(Tool):
    """A tool whose reply comes from the scenario, and whose call is logged."""

    scenario: sr.Scenario
    call_log: Path
    history: dict

    async def run(self, arguments: dict) -> ToolResult:
        kind = sr.call_kind(self.name, arguments)
        earlier = self.history.setdefault(f"{self.name}.{kind}", [])
        reply = sr.pick_reply(self.scenario, self.name, arguments, len(earlier), earlier)
        earlier.append(arguments)
        with self.call_log.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"t": time.time(), "tool": self.name, "kind": kind, "arguments": arguments}
                )
                + "\n"
            )
        return ToolResult(content=json.dumps(reply, indent=2))


def build_server(
    scenario: sr.Scenario,
    call_log: Path,
    *,
    product_root: Path = sr.REPO_ROOT,
    instructions: str | None = None,
) -> FastMCP:
    specs = sr.tool_specs(product_root)
    missing = [name for name in scenario.served_tools if name not in specs]
    if missing:
        raise SystemExit(f"scenario {scenario.id} serves tools with no schema: {missing}")
    server = FastMCP("exomem", instructions=instructions)
    history: dict = {}
    for name in scenario.served_tools:
        spec = specs[name]
        server.add_tool(
            ScriptedTool(
                name=name,
                description=spec["description"],
                parameters=spec["inputSchema"],
                scenario=scenario,
                call_log=call_log,
                history=history,
            )
        )
    return server


if __name__ == "__main__":
    recorded = (
        json.loads(Path(os.environ["IG_SHAPES"]).read_text(encoding="utf-8"))
        if os.environ.get("IG_SHAPES")
        else {}
    )
    build_server(
        sr.load_scenario(Path(os.environ["IG_SCENARIO"]), recorded.get("shapes")),
        Path(os.environ["IG_CALL_LOG"]),
        product_root=Path(os.environ.get("IG_PRODUCT_ROOT", sr.REPO_ROOT)),
        instructions=recorded.get("instructions"),
    ).run(transport="stdio", show_banner=False)
