"""Every call that the shipped skills spell is one the published tool surface takes.

An agent copies a call from skill text as written. When the tool surface changes and the skill text does
not, the agent sends fields the tool refuses or names operations that no door serves. This test reads every
code span and code block of the shipped skill text, parses each call with Python's parser, and checks it
against the published tools and door operations. The surface is the single source; the skills follow it.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path

import pytest

from markdown_it import MarkdownIt

from exomem import commands
from exomem.public_surface import DOORS, PASSTHROUGH_COMMANDS, help_for, public_surface

SCHEMA = Path(__file__).resolve().parents[1] / "src" / "exomem" / "_scaffold" / "_Schema"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "mcp_tool_schemas.json"

# A door operation is spelled `<area>.<action>` in lowercase snake case: the format the doors publish.
_OPERATION_SHAPE = re.compile(r"^[a-z_]+\.[a-z_-]+$")

# Calls the skills name that are a harness's own discovery tools, not Evermatter tools.
_HARNESS_CALLS = frozenset({"ToolSearch"})  # nosemgrep: ep-word-set -- the closed set of harness calls the skill text names.


def _shipped_files() -> list[Path]:
    return sorted([SCHEMA / "SKILL.md", *(SCHEMA / "references").glob("*.md"),
                   *(SCHEMA / "workflow-skills").glob("*/SKILL.md")])


def _spans(path: Path) -> Iterator[tuple[int, str]]:
    """Every code span and every line of a code block, with its line number, as CommonMark parses the file.

    An inline span that wraps onto the next line arrives as one span, its line break read as a space.
    """
    for token in MarkdownIt("commonmark").parse(path.read_text(encoding="utf-8")):
        if token.type in ("fence", "code_block") and token.map:
            for offset, line in enumerate(token.content.splitlines()):
                yield token.map[0] + 1 + (offset + 1 if token.type == "fence" else offset), line.strip()
        elif token.type == "inline" and token.map:
            for child in token.children or ():
                if child.type == "code_inline":
                    yield token.map[0] + 1, child.content.strip()


@cache
def _tool_fields() -> dict[str, frozenset[str]]:
    tools = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return {name: frozenset(tool["inputSchema"].get("properties", {})) for name, tool in tools.items()}


@cache
def _door_operations() -> dict[str, frozenset[str]]:
    return {door: frozenset(ops) for door, ops in public_surface().doors.items()}


@cache
def _operation_fields(name: str) -> frozenset[str] | None:
    """The argument names one door operation accepts, or None when its schema leaves them open."""
    reply = help_for(name, commands.PRODUCT_COMMANDS)
    replies = reply.get("tools", [reply])
    fields: set[str] = set()
    for one in replies:
        fields.update(one.get("arguments", {}))
        for alternative in one.get("alternatives", ()):
            fields.update(alternative.get("properties", {}))
    return frozenset(fields) or None


@cache
def _internal_names() -> tuple[frozenset[str], frozenset[str]]:
    """Grouped command names and their internal kinds: what the doors hide behind `<area>.<action>`."""
    ops = [op for door in public_surface().doors.values() for op in door.values()]
    names = {op.name for op in ops}
    return (frozenset(op.command for op in ops) - set(_tool_fields()),
            frozenset(op.kind for op in ops if "-" in op.kind) - names)


# Skill text writes a value the agent fills as `<placeholder>` or `...`; both stand for one expression.
_PLACEHOLDER = re.compile(r"<[^<>`]*>")
_TRAILING_ELLIPSIS = re.compile(r",\s*\.\.\.\s*\)")


def _calls(text: str) -> list[ast.Call]:
    text = _TRAILING_ELLIPSIS.sub(")", _PLACEHOLDER.sub("_", text))
    try:
        tree = ast.parse(text, mode="eval")
    except SyntaxError:
        return []
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]


def _constant_keys(node: ast.expr) -> list[str]:
    if not isinstance(node, ast.Dict):
        return []
    return [key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)]


def _call_problems(call: ast.Call) -> list[str]:
    name = call.func.id
    tools = _tool_fields()
    if name in _HARNESS_CALLS or (name in PASSTHROUGH_COMMANDS.values() and name not in tools):
        # A passthrough tool outside the fixture is published only by the Cloud gateway; its fields are its own.
        return []
    if name not in tools:
        return [f"`{name}(...)` is not a published tool"]
    problems = [f"`{name}` takes no field `{keyword.arg}`" for keyword in call.keywords
                if keyword.arg is not None and keyword.arg not in tools[name]]
    if name not in DOORS.values():
        return problems
    operation = next((k.value for k in call.keywords if k.arg == "operation"), None)
    if not (isinstance(operation, ast.Constant) and isinstance(operation.value, str)):
        return problems
    if operation.value not in _door_operations()[name]:
        return [*problems, f"`{name}` serves no operation `{operation.value}`"]
    accepted = _operation_fields(operation.value)
    arguments = next((k.value for k in call.keywords if k.arg == "arguments"), None)
    if accepted is not None and arguments is not None:
        problems += [f"`{operation.value}` takes no argument `{key}`"
                     for key in _constant_keys(arguments) if key not in accepted]
    return problems


def _span_problems(span: str) -> list[str]:
    problems = [problem for call in _calls(span) for problem in _call_problems(call)]
    commands_hidden, kinds_hidden = _internal_names()
    all_operations = set().union(*_door_operations().values())
    areas = {name.split(".", 1)[0] for name in all_operations}
    if _OPERATION_SHAPE.match(span) and span.split(".", 1)[0] in areas and span not in all_operations:
        problems.append(f"`{span}` is not a door operation")
    if span in commands_hidden:
        problems.append(f"`{span}` is an internal command name; name the tool or the door operation")
    if span in kinds_hidden:
        problems.append(f"`{span}` is an internal kind; spell the door operation `<area>.<action>`")
    return problems


def _findings() -> list[str]:
    findings = []
    for path in _shipped_files():
        for number, span in _spans(path):
            findings += [f"{path.relative_to(SCHEMA)}:{number}: {problem}" for problem in _span_problems(span)]
    return findings


def test_every_call_in_the_shipped_skills_is_one_the_surface_takes() -> None:
    findings = _findings()
    assert not findings, "\n".join(findings)


def test_the_check_reads_calls_written_with_placeholders() -> None:
    assert _span_problems('recall_context(turn=<the message>, rerank=<x>, ...)')


@pytest.mark.parametrize(("span", "problem"), [
    ('search_memory(query="x", rerank=False)', "takes no field `rerank`"),
    ('inspect_memory(operation="curation.work-item")', "serves no operation"),
    ('manage_memory(operation="records.retract", arguments={"nope": 1})', "takes no argument `nope`"),
    ("move_file(path='a')", "is not a published tool"),
    ("resolve-relation", "internal kind"),
])
def test_the_check_names_each_kind_of_drift(span: str, problem: str) -> None:
    assert any(problem in found for found in _span_problems(span))
