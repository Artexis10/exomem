"""Scenario files and the scripted-reply choice."""

from __future__ import annotations

import functools
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import checks, status_shape

SCENARIO_DIR = Path(__file__).resolve().parent / "scenarios"
REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_FILES = ("mcp_tool_schemas.json", "mcp_cloud_only_tool_schemas.json")
#: The suite a scenario belongs to. `import` scenarios sit in `scenarios/`; any other suite in `scenarios/<suite>/`.
DEFAULT_SUITE = "import"


def scenario_dir(suite: str) -> Path:
    return SCENARIO_DIR if suite == DEFAULT_SUITE else SCENARIO_DIR / suite


@functools.cache
def tool_specs(product_root: Path = REPO_ROOT) -> dict[str, dict]:
    """Every tool's shipped description and input schema, from the two test fixtures of one checkout.
    Read-only: it is cached per checkout, so each arm of a comparison shows the agent its own tool text."""
    specs: dict[str, dict] = {}
    for name in SCHEMA_FILES:
        specs.update(
            json.loads((product_root / "tests" / "fixtures" / name).read_text(encoding="utf-8"))
        )
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
    suite: str = DEFAULT_SUITE
    # The scenario file; the scripted server loads the same file the runner did.
    path: Path | None = None
    # `transcript`: the judge reads the whole run. `answer`: it reads the person's turn, the `reference`
    # and the agent's final answer only, so no tool reply can show it which arm it is judging.
    judge_view: str = "transcript"
    reference: str = ""
    # Real tool calls, made on each arm's own code to record the shapes the scripted replies name with `$shape`.
    record: dict = field(default_factory=dict)


def resolve_shapes(reply: Any, shapes: Mapping[str, Any] | None) -> Any:
    """Replace `{"$shape": name}` with the reply recorded from the arm's own code. With no recording the
    reply stays as written: the runner reads only a scenario's prompt, checks and criteria."""
    if isinstance(reply, list):
        return [resolve_shapes(item, shapes) for item in reply]
    if shapes is None:
        return reply
    if isinstance(reply, dict) and set(reply) == {"$shape"}:
        if reply["$shape"] not in shapes:
            raise SystemExit(f"reply names shape {reply['$shape']!r}, which is not recorded")
        return shapes[reply["$shape"]]
    if isinstance(reply, dict) and set(reply) == {"$rules"}:
        return {
            "$rules": [
                {**rule, "reply": resolve_shapes(rule["reply"], shapes)} for rule in reply["$rules"]
            ]
        }
    return reply


def load_scenario(path: Path, shapes: Mapping[str, Any] | None = None) -> Scenario:
    """`shapes` maps a shape name to the reply recorded from one arm; only a scenario that names a `$shape` needs it."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return Scenario(
        id=raw["id"],
        title=raw["title"],
        prompt=raw["prompt"],
        served_tools=tuple(raw["served_tools"]),
        builtin_tools=tuple(raw.get("builtin_tools", ())),
        replies={
            key: status_shape.expand(resolve_shapes(reply, shapes))
            for key, reply in raw["replies"].items()
        },
        default_reply=raw["default_reply"],
        checks=tuple(raw.get("checks", ())),
        criteria=tuple(raw.get("criteria", ())),
        fake_commands=dict(raw.get("fake_commands", {})),
        source_folder=raw.get("source_folder"),
        suite=raw.get("suite", DEFAULT_SUITE),
        path=Path(path),
        judge_view=raw.get("judge_view", "transcript"),
        reference=raw.get("reference", ""),
        record=dict(raw.get("record", {})),
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


def _rule_index(rules: list[dict], arguments: dict) -> int:
    """The first rule whose `when` fits the arguments; a rule with no `when` fits any call."""
    for index, rule in enumerate(rules):
        if checks.matches(arguments, rule.get("when", {})):
            return index
    raise LookupError("no rule fits the call")


def _pick_rule(rules: list[dict], arguments: dict, history: Sequence[dict]) -> Any:
    """A rule's reply may be a sequence, advanced by the earlier calls that the same rule answered."""
    index = _rule_index(rules, arguments)
    reply = rules[index]["reply"]
    if not isinstance(reply, list):
        return reply
    answered = sum(_rule_index(rules, earlier) == index for earlier in history)
    return reply[min(answered, len(reply) - 1)]


def pick_reply(
    scenario: Scenario, tool: str, arguments: dict, prior_calls: int, history: Sequence[dict] = ()
) -> Any:
    """Reply for the `prior_calls`-th (0-based) call of this tool and kind.

    Key order: `tool.kind`, then `tool`, then the scenario default. A list is a
    sequence: the nth call gets the nth item and the last item repeats. A `$rules` entry picks by the
    call's arguments instead, so a validate-only call and a commit get different replies whatever
    their order; `history` holds the arguments of the earlier calls of this tool and kind.
    """
    kind = call_kind(tool, arguments)
    for key in (f"{tool}.{kind}" if kind else None, tool):
        if key is not None and key in scenario.replies:
            entry = scenario.replies[key]
            if isinstance(entry, dict) and set(entry) == {"$rules"}:
                return _pick_rule(entry["$rules"], arguments, history)
            if isinstance(entry, list):
                return entry[min(prior_calls, len(entry) - 1)]
            return entry
    return scenario.default_reply
