"""Scenario files and the scripted-reply choice."""

from __future__ import annotations

import functools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import status_shape

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"
FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
SCHEMA_FILES = ("mcp_tool_schemas.json", "mcp_cloud_only_tool_schemas.json")


@functools.cache
def tool_specs() -> dict[str, dict]:
    """Every tool's shipped description and input schema, from the two test fixtures. Read-only: it is cached."""
    specs: dict[str, dict] = {}
    for name in SCHEMA_FILES:
        specs.update(json.loads((FIXTURES / name).read_text(encoding="utf-8")))
    return specs


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    prompt: str
    served_tools: tuple[str, ...]
    builtin_tools: tuple[str, ...]
    replies: dict[str, Any]
    default_reply: Any
    checks: tuple[dict, ...]
    criteria: tuple[dict, ...]
    fake_commands: dict[str, str] = field(default_factory=dict)
    # A folder the prompt names, created in the run's working directory so a shell agent finds it.
    source_folder: str | None = None


def load_scenario(path: Path) -> Scenario:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Scenario(
        id=raw["id"],
        title=raw["title"],
        prompt=raw["prompt"],
        served_tools=tuple(raw["served_tools"]),
        builtin_tools=tuple(raw.get("builtin_tools", ())),
        replies={key: status_shape.expand(reply) for key, reply in raw["replies"].items()},
        default_reply=raw["default_reply"],
        checks=tuple(raw.get("checks", ())),
        criteria=tuple(raw.get("criteria", ())),
        fake_commands=dict(raw.get("fake_commands", {})),
        source_folder=raw.get("source_folder"),
    )


def call_kind(tool: str, arguments: dict) -> str | None:
    """The operation a call selects: `operation.kind`, else `action`, else the tool schema's default action."""
    operation = arguments.get("operation")
    if isinstance(operation, dict) and isinstance(operation.get("kind"), str):
        return operation["kind"]
    action = arguments.get("action")
    if action is None:
        schema = tool_specs().get(tool, {}).get("inputSchema", {})
        action = schema.get("properties", {}).get("action", {}).get("default")
    return action if isinstance(action, str) else None


def pick_reply(scenario: Scenario, tool: str, arguments: dict, prior_calls: int) -> Any:
    """Reply for the `prior_calls`-th (0-based) call of this tool and kind.

    Key order: `tool.kind`, then `tool`, then the scenario default. A list is a
    sequence: the nth call gets the nth item and the last item repeats.
    """
    kind = call_kind(tool, arguments)
    for key in (f"{tool}.{kind}" if kind else None, tool):
        if key is not None and key in scenario.replies:
            entry = scenario.replies[key]
            if isinstance(entry, list):
                return entry[min(prior_calls, len(entry) - 1)]
            return entry
    return scenario.default_reply
