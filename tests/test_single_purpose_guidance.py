"""Guidance names only the tools that the active surface lists.

Hosts let a model call only the tools a server lists. The single-purpose-tools pilot lists
`read_records`, `change_records`, `read_plans` and `change_plans` on MCP and hides the
grouped `record_memory` and `plan_memory`, so nothing a server sends or ships to an MCP
client may tell it to call a hidden name. REST and the CLI keep the grouped names.

The workflow test drives a real MCP server per shape: a model bootstraps, files a note that
a Records collection wants, receives a proposed call, and makes it.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from fastmcp import Client
from starlette.testclient import TestClient
from test_incident_records_routing import INCIDENTS, LEDGER, _incident_vault, _terminal_routing

from exomem import (
    call_ledger,
    capabilities,
    commands,
    knowledge_packs,
    mutation_terminal,
    operation_entries,
    product_invoke,
    writer_lease,
)
from exomem import server as server_module
from exomem._hooks import exomem_capture_nudge as capture_hook

SHAPES = ("nested",)
GROUPED_NAMES = frozenset({"record_memory", "plan_memory"})
RENDERED_NAMES = frozenset(
    spec.name for name, operations in operation_entries.OPERATIONS.items()
    if name in GROUPED_NAMES for spec in operations.renderings
)
ALL_SPELLINGS = GROUPED_NAMES | RENDERED_NAMES

# What a client of each shape is told to call for a filing, written from the design.
FILING_TOOL = {"nested": "change_records", "grouped": "record_memory"}

REPOSITORY = Path(__file__).resolve().parents[1]
SCAFFOLD = REPOSITORY / "src" / "exomem" / "_scaffold"


def _mentions(text: str, names: frozenset[str] | set[str]) -> set[str]:
    return {name for name in names if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text)}


def _texts(value: object) -> list[str]:
    """Every string and every key in a JSON-shaped value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for key, child in value.items() for text in (str(key), *_texts(child))]
    if isinstance(value, (list, tuple)):
        return [text for child in value for text in _texts(child)]
    return []


@pytest.fixture
def build(vault, monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    monkeypatch.delenv("EXOMEM_MCP_TOOL_SHAPE", raising=False)
    monkeypatch.delenv("EXOMEM_MCP_LEGACY_COMPAT", raising=False)
    call_ledger.reset_chain_cache()

    def make(shape: str):
        monkeypatch.setenv("EXOMEM_MCP_TOOL_SHAPE", shape)
        return server_module.build_server(require_auth=False)

    yield make
    writer_lease.reset_managers_for_tests()


async def _call(client: Client, name: str, arguments: dict) -> dict:
    result = await client.call_tool(name, arguments, raise_on_error=False)
    assert result.structured_content is not None, result.content
    return result.structured_content


# --- the workflow, once per shape ---------------------------------------------------------


@pytest.mark.parametrize("shape", SHAPES)
def test_a_model_is_guided_to_a_listed_tool_and_the_proposed_call_succeeds(
    vault, build, monkeypatch, shape
) -> None:
    evidence = _incident_vault(vault, monkeypatch, "maximal")
    mcp = build(shape)
    note = {
        "content": "## Observations\n\n- [failure] The panel stopped responding after load #widget-app ^froze\n",
        "title": "Widget panel froze on load",
        "slug": "widget-panel-froze",
        "note_type": "failure",
        "projects": ["widget-app"],
        "tags": ["widget-app", "failure"],
        "sources": [evidence],
    }

    async def scenario():
        async with Client(mcp) as client:
            tools = await client.list_tools()
            bootstrap = await _call(client, "bootstrap", {"profile": "full"})
            validation = await _call(client, "create_note", {**note, "validate_only": True})
            written = await _call(
                client,
                "create_note",
                {
                    **note,
                    "draft_id": validation["draft_id"],
                    "draft_hash": validation["draft_hash"],
                    "draft_token": validation["draft_token"],
                    "relation_disposition": "reviewed_none",
                    "relation_review_hash": validation["draft_hash"],
                    "relation_review_reason": "No honest relation exists for this fixture.",
                },
            )
            routing = written["records_routing"]
            (call_tool,) = [key for key in routing if key in ALL_SPELLINGS]
            followed = await _call(client, call_tool, routing[call_tool])
            return tools, bootstrap, routing, call_tool, followed

    tools, bootstrap, routing, call_tool, followed = asyncio.run(scenario())

    listed = {tool.name for tool in tools}
    hidden = ALL_SPELLINGS - listed
    assert hidden, "the shape must hide some spelling, or this test proves nothing"
    # Nothing the model reads names a spelling it cannot call: not the tool schemas, the
    # bootstrap, or the disposition that asks it to file the note.
    for tool in tools:
        assert not _mentions(f"{tool.description} {json.dumps(tool.input_schema)}", hidden), tool.name
    assert not _mentions("\n".join(_texts(bootstrap)), hidden)
    assert not _mentions("\n".join(_texts(routing)), hidden)

    assert call_tool == FILING_TOOL[shape]
    assert call_tool in listed
    assert call_tool in routing["instruction"]
    arguments = routing[call_tool]
    if shape == "nested":
        assert set(arguments) == {"operation"}
        assert arguments["operation"]["kind"] == "append"
        assert arguments["operation"]["collection"] == INCIDENTS
    else:
        assert arguments["action"] == "append"
        assert arguments["collection"] == INCIDENTS
    assert followed["operation"] == "append", followed
    assert followed["item_key"]


@pytest.mark.parametrize("shape", SHAPES)
def test_contract_and_refusal_responses_name_only_listed_tools(vault, build, monkeypatch, shape) -> None:
    """The describe contract, the query and import routes and a generic-write refusal carry call text."""
    _incident_vault(vault, monkeypatch, "maximal")
    mcp = build(shape)
    appended = {
        "action": "append",
        "collection": INCIDENTS,
        "item": {"incident": "Widget panel froze", "observed_on": "2026-09-27"},
        "why": "Recurring incident observed",
    }

    async def scenario():
        responses = []
        async with Client(mcp) as client:
            listed = {tool.name for tool in await client.list_tools()}

            async def call(command: str, arguments: dict):
                tool, spelled = operation_entries.spell_call(command, arguments, shape)
                assert tool in listed
                result = await client.call_tool(tool, spelled, raise_on_error=False)
                responses.extend([result.structured_content, [part.text for part in result.content]])
                return result.structured_content

            await call("record_memory", {"action": "describe"})
            await call("record_memory", {"action": "describe", "collection": INCIDENTS})
            await call("record_memory", appended)
            (item,) = (vault / Path(INCIDENTS).parent / "Entries").glob("*.md")
            refusal = await client.call_tool(
                "edit_note",
                {
                    "path": str(item.relative_to(vault)),
                    "operation": {"kind": "replace_body", "new_body": "Changed outside the owning product."},
                    "why": "exercise the generic-write refusal",
                },
                raise_on_error=False,
            )
            responses.extend([refusal.structured_content, [part.text for part in refusal.content]])
        return listed, responses

    listed, responses = asyncio.run(scenario())

    hidden = ALL_SPELLINGS - listed
    assert hidden
    text = "\n".join(_texts(responses))
    assert "STRUCTURED_ITEM_REQUIRES_PRODUCT_ROUTE" in text
    assert not _mentions(text, hidden)


@pytest.mark.parametrize("shape", SHAPES)
def test_bootstrap_routes_name_the_tools_of_the_shape(vault, build, shape) -> None:
    mcp = build(shape)

    async def scenario():
        async with Client(mcp) as client:
            return await _call(client, "bootstrap", {"profile": "full"})

    payload = asyncio.run(scenario())

    records = payload["records"]
    planning = payload["planning"]
    if shape == "grouped":
        assert records["route"]["tool"] == "record_memory"
        assert planning["route"]["tool"] == "plan_memory"
        assert records["contract_route"] == {"tool": "record_memory", "arguments": {"action": "describe"}}
    else:
        assert records["route"]["selector"] == planning["route"]["selector"] == "operation.kind"
        assert records["route"]["tools"] == {
            "read_records": ["describe", "validate", "inspect", "query"],
            "change_records": ["create", "append", "update", "revise", "rebaseline"],
        }
        assert planning["route"]["tools"] == {
            "read_plans": ["inspect", "query"],
            "change_plans": ["create", "add", "update", "triage"],
        }
        described = records["contract_route"]
        assert described["tool"] == "read_records"
        assert described["arguments"] == {"operation": {"kind": "describe"}}


# --- REST and the CLI keep the grouped names ----------------------------------------------


def test_rest_and_cli_bootstrap_still_name_the_grouped_commands(vault, monkeypatch) -> None:
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_REST_API_KEY", "sekret")
    monkeypatch.setenv("EXOMEM_MCP_TOOL_SHAPE", "nested")
    rest = TestClient(server_module.build_server(require_auth=False).http_app())
    served = rest.post(
        "/api/bootstrap", json={"profile": "full"}, headers={"Authorization": "Bearer sekret"}
    ).json()
    with capabilities.active_surface(product_invoke.cli_surface_descriptor()):
        cli = commands.op_bootstrap(vault, profile="full")

    for surface, payload in {"rest": served, "cli": cli}.items():
        text = "\n".join(_texts(payload))
        assert _mentions(text, GROUPED_NAMES) == GROUPED_NAMES, surface
        assert not _mentions(text, RENDERED_NAMES), surface


# --- the disposition's call is validated as before, in every shape ------------------------


def _surface(shape: str) -> capabilities.ActiveSurfaceDescriptor:
    return capabilities.ActiveSurfaceDescriptor(
        surface="mcp", profile="product", tier2_enabled=True, product_commands=(), tool_shape=shape
    )


_ITEM = {"incident": "Widget panel froze", "observed_on": "2026-09-27"}


def _spelled(shape: str, **changes: object) -> tuple[str, dict]:
    call = {"action": "append", "collection": INCIDENTS, "item": _ITEM, "why": "Recurring incident", **changes}
    if shape == "nested":
        kind = call.pop("action")
        return "change_records", {"operation": {"kind": kind, **call}}
    return FILING_TOOL[shape], call


def _routing(tool: str, call: object) -> dict:
    return {
        "collection": INCIDENTS,
        "title": "Widget incidents",
        "matched_terms": [],
        "matched_predicates": ["project:widget-app", "type:failure"],
        "natural_key": ["incident"],
        "strength": "strong",
        "disposition": "file",
        "signal_version": "0123456789abcdef",
        "instruction": f"Perform this {tool} call now, without asking.",
        tool: call,
    }


@pytest.mark.parametrize("shape", SHAPES)
def test_the_terminal_carries_a_well_formed_call_spelled_for_the_shape(shape) -> None:
    tool, call = _spelled(shape)
    routing = _routing(tool, call)

    with capabilities.active_surface(_surface(shape)):
        assert _terminal_routing(routing) == routing


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize(
    "changes",
    [
        {"action": "discard"},
        {"collection": LEDGER},
        {"why": "x" * 5000},
    ],
    ids=["action-outside-append-and-update", "other-collection", "oversize"],
)
def test_the_terminal_drops_a_malformed_call_in_every_shape(shape, changes) -> None:
    tool, call = _spelled(shape, **changes)

    with capabilities.active_surface(_surface(shape)):
        assert _terminal_routing(_routing(tool, call)) is None


@pytest.mark.parametrize("shape", SHAPES)
def test_the_size_bound_admits_the_same_item_in_every_shape(shape) -> None:
    """The wrapper that a shape adds to the call does not move the bound."""
    limit = mutation_terminal._MAX_ROUTING_CALL_BYTES
    _tool, bare = _spelled("grouped", why="")
    padding = limit - len(json.dumps(bare, ensure_ascii=False, sort_keys=True).encode())

    at_the_bound = _spelled(shape, why="x" * padding)
    over_the_bound = _spelled(shape, why="x" * (padding + 1))

    with capabilities.active_surface(_surface(shape)):
        assert _terminal_routing(_routing(*at_the_bound)) is not None
        assert _terminal_routing(_routing(*over_the_bound)) is None


@pytest.mark.parametrize("shape,other", [("nested", "grouped"), ("grouped", "nested")])
def test_the_terminal_drops_a_call_spelled_for_another_shape(shape, other) -> None:
    tool, call = _spelled(other)

    with capabilities.active_surface(_surface(shape)):
        assert _terminal_routing(_routing(tool, call)) is None


# --- shipped guidance ---------------------------------------------------------------------


def _scaffold_texts() -> dict[str, str]:
    return {
        str(path.relative_to(SCAFFOLD)): path.read_text(encoding="utf-8")
        for path in sorted(SCAFFOLD.rglob("*"))
        if path.is_file() and path.suffix in {".md", ".yaml", ".yml", ".json"}
    }


def test_the_shipped_skill_packs_and_hook_name_no_grouped_spelling() -> None:
    """The skill ships static, so it names the default `nested` renderings; a pack is spelt when served."""
    named = {path: _mentions(text, GROUPED_NAMES) for path, text in _scaffold_texts().items()}
    with capabilities.active_surface(_surface("nested")):
        served = knowledge_packs.list_builtin_packs()
    named |= {
        f"pack {pack['id']}": _mentions("\n".join(_texts(pack)), GROUPED_NAMES) for pack in served
    }
    hook_texts = {
        name: value
        for name, value in vars(capture_hook).items()
        if name.isupper() and isinstance(value, str)
    }
    named |= {f"hook.{name}": _mentions(text, GROUPED_NAMES) for name, text in hook_texts.items()}

    assert {path: names for path, names in named.items() if names} == {}


def test_every_call_the_skill_shows_names_an_operation_that_tool_carries() -> None:
    carried = {
        spec.name: {entry.action for entry in operations.entries if entry.rendering == spec.name}
        for operations in operation_entries.OPERATIONS.values()
        for spec in operations.renderings
    }
    call = re.compile(rf'(?<!\w)({"|".join(carried)})\(operation=\{{"kind": "(\w+)"')
    shown = [
        (path, tool, kind)
        for path, text in _scaffold_texts().items()
        for tool, kind in call.findall(text)
    ]

    assert shown, "the skill shows at least one single-purpose call"
    assert [(path, tool, kind) for path, tool, kind in shown if kind not in carried[tool]] == []


# --- the capture-nudge hook ---------------------------------------------------------------


def test_the_hook_stamps_exactly_the_write_selectors_of_the_entries() -> None:
    assert capture_hook._COLLECTION_WRITES == operation_entries.write_selectors(), (
        "restamp _COLLECTION_WRITES in exomem_capture_nudge.py from operation_entries.write_selectors():\n"
        + "\n".join(f'    "{selector}",' for selector in operation_entries.write_selectors())
    )


def _hook_sees_a_write(tool: str, arguments: dict) -> bool:
    return capture_hook._successful_kb_write({"name": f"mcp__exomem__{tool}", "input": arguments})


def _grouped_arguments(operations, entry) -> dict:
    """The grouped arguments that select one entry: its action and, where it has one, its mode."""
    rendering = next(item for item in operations.renderings_with_entries() if item.name == entry.rendering)
    return rendering.arguments_for(entry)


@pytest.mark.parametrize("shape", SHAPES)
def test_the_hook_counts_every_write_and_no_read_of_each_shape(shape) -> None:
    """Each operation of each tool, called as a client of this shape, is a write or a read as its entry says."""
    for name, operations in operation_entries.OPERATIONS.items():
        if name not in GROUPED_NAMES:
            continue
        for entry in operations.entries:
            arguments = _grouped_arguments(operations, entry)
            tool, spelled = operation_entries.spell_call(operations.command, arguments, shape)
            # A grouped import is a read or a write by mode, which the hook cannot tell
            # apart, so it leaves the grouped import to the other signals.
            ambiguous = shape == "grouped" and entry.action == "import"
            if ambiguous:
                continue
            assert _hook_sees_a_write(tool, spelled) is (entry.effect == "write"), (shape, tool, spelled)


def test_the_hook_ignores_a_failed_or_malformed_collection_write() -> None:
    failed = {"name": "mcp__exomem__change_records", "input": {"operation": {"kind": "append"}}, "failed": True}
    bare = {"name": "mcp__exomem__change_records", "input": {}}
    other_server = {"name": "mcp__other__change_records", "input": {"operation": {"kind": "append"}}}

    assert not capture_hook._successful_kb_write(failed)
    assert not capture_hook._successful_kb_write(bare)
    assert not capture_hook._successful_kb_write(other_server)
