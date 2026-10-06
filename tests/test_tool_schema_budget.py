"""The MCP tool surface stays under a committed byte budget.

Clients that resend every tool schema each turn pay for whatever prose a
description carries, so the complete wire surface has a total budget and a
per-tool ceiling (`openspec/changes/shrink-tool-descriptions`). Raise a ceiling
deliberately, in the diff that needs it, never as a side effect.

Sizes are the compact UTF-8 JSON bytes of each tool's wire object, measured by
`scripts/measure-tool-schema-bytes.py` so the script and this gate cannot
disagree.
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
from pathlib import Path

import pytest
from fastmcp.server.dependencies import without_injected_parameters
from fastmcp.tools.function_parsing import get_cached_typeadapter  # noqa: F401
from test_mcp_schema_fidelity import _build_server

REPO_ROOT = Path(__file__).resolve().parents[1]

TOTAL_BUDGET = 90_000

#: Per-tool ceilings in bytes. Their sum is deliberately under TOTAL_BUDGET.
TOOL_CEILINGS: dict[str, int] = {
    "edit_memory": 6725,
    "remember": 5200,
    "replace_memory": 4600,
    "manage_memory_file": 4325,
    "connect_memory": 4300,
    "episode_memory": 4300,
    "record_memory": 4275,
    "ask_memory": 4125,
    "schema_memory": 4000,
    "observe_memory": 3850,
    "maintain_memory": 3825,
    "adoption_studio": 3400,
    "capture_source": 3475,
    "review_memory": 3375,
    "activate_context": 3000,
    "triage_memory": 3100,
    "govern_memory": 3225,
    "plan_memory": 2400,
    "preserve_artifacts": 2425,
    "configure_memory": 1700,
    "read_memory": 1550,
    "adopt_vault": 1500,
    "preserve_evidence": 1500,
    "review_item_context": 1500,
    "bootstrap": 1400,
    "query_dataset": 1375,
    "process_media": 1200,
    "transfer_artifact": 1100,
    "browse_memory": 1000,
    "compile_source": 825,
    "read_media": 800,
    "coordination_status": 600,
}

AUTHORING_TOOLS = frozenset(
    {"remember", "replace_memory", "edit_memory", "observe_memory", "manage_memory_file"}
)


def _measure_module():
    spec = importlib.util.spec_from_file_location(
        "measure_tool_schema_bytes", REPO_ROOT / "scripts" / "measure-tool-schema-bytes.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return _build_server(monkeypatch, tmp_path)


def _wires(server) -> list[dict]:
    return [
        tool.to_mcp_tool().model_dump(mode="json", by_alias=True, exclude_none=True)
        for tool in asyncio.run(server.list_tools())
    ]


def test_total_wire_bytes_stay_under_budget(server) -> None:
    rows = _measure_module().measure(_wires(server))
    total = sum(row["total"] for row in rows)
    worst = ", ".join(f"{r['name']}={r['total']:,}" for r in rows[:5])
    assert total <= TOTAL_BUDGET, f"tool surface is {total:,} B, budget {TOTAL_BUDGET:,} B; largest: {worst}"


def test_every_tool_stays_under_its_ceiling(server) -> None:
    rows = _measure_module().measure(_wires(server))
    unbudgeted = sorted(row["name"] for row in rows if row["name"] not in TOOL_CEILINGS)
    assert not unbudgeted, f"tools without a ceiling: {unbudgeted}"
    over = [
        f"{row['name']}: {row['total']:,} > {TOOL_CEILINGS[row['name']]:,}"
        for row in rows
        if row["total"] > TOOL_CEILINGS[row["name"]]
    ]
    assert not over, "over ceiling: " + "; ".join(over)
    assert sum(TOOL_CEILINGS.values()) <= TOTAL_BUDGET


def test_semantic_authoring_contract_is_carried_once_per_authoring_tool(server) -> None:
    marker = re.compile(r"Semantic authoring \[exomem\.semantic-authoring:v5 sha256:[0-9a-f]{64}\]")
    for wire in _wires(server):
        in_description = len(marker.findall(wire["description"]))
        in_parameters = len(marker.findall(str(wire["inputSchema"])))
        expected = 1 if wire["name"] in AUTHORING_TOOLS else 0
        assert in_description == expected, wire["name"]
        assert in_parameters == 0, f"{wire['name']} repeats the contract in a parameter"
        if wire["name"] in AUTHORING_TOOLS:
            from exomem.semantic_authoring import AUTHORING_CONTRACT

            for code in AUTHORING_CONTRACT.findings:
                assert code in wire["description"], (wire["name"], code)


def test_published_schemas_do_not_advertise_optional_null(server) -> None:
    def walk(node, path):
        if isinstance(node, dict):
            for name, prop in node.get("properties", {}).items():
                if name in node.get("required", []):
                    continue
                assert prop.get("default", 0) is not None, f"default null at {path}/{name}"
                assert {"type": "null"} not in prop.get("anyOf", []), (
                    f"optional null arm at {path}/{name}"
                )
            for key, value in node.items():
                walk(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    for wire in _wires(server):
        walk(wire["inputSchema"], wire["name"])


def test_compaction_preserves_required_and_array_item_nullability() -> None:
    from jsonschema import validate

    from exomem.command_surface import compact_input_schema

    nullable = {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}
    schema = {
        "type": "object",
        "required": ["selected"],
        "properties": {
            "selected": nullable,
            "values": {"type": "array", "items": nullable},
            "optional": nullable,
        },
    }
    compact = compact_input_schema(schema)
    validate({"selected": None, "values": [None, "value"]}, compact)
    assert compact["properties"]["selected"] == nullable
    assert compact["properties"]["values"]["items"] == nullable
    assert compact["properties"]["optional"] == {"type": "string"}


def test_every_nullable_optional_parameter_still_accepts_an_explicit_null(server) -> None:
    """Dropping the null arm from the published schema must not narrow the server."""
    checked = 0
    for tool in asyncio.run(server.list_tools()):
        adapter = get_cached_typeadapter(without_injected_parameters(tool.fn))
        declared = adapter.json_schema()
        for name, prop in declared.get("properties", {}).items():
            arms = prop.get("anyOf") or []
            if {"type": "null"} not in arms or name in declared.get("required", []):
                continue
            try:
                adapter.validate_python({name: None})
            except Exception as exc:  # noqa: BLE001 - inspect pydantic's error locations
                locations = [error["loc"][:1] for error in getattr(exc, "errors", lambda: [])()]
                assert (name,) not in [tuple(loc) for loc in locations], (
                    f"{tool.name}.{name} rejects an explicit null: {exc}"
                )
            checked += 1
    assert checked > 300, checked


def test_cli_help_and_rest_point_to_the_contract_but_the_mcp_wire_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI and REST are not agent context: one pointer line, never on the MCP wire."""
    from starlette.testclient import TestClient

    from exomem import semantic_authoring
    from exomem.__main__ import main

    monkeypatch.setenv("EXOMEM_REST_API_KEY", "synthetic-test-key")
    server = _build_server(monkeypatch, tmp_path)
    pointer = semantic_authoring.CLI_REST_POINTER
    assert 'bootstrap(profile="full")' in pointer and "exomem bootstrap --profile full" in pointer
    assert all(pointer not in str(wire) for wire in _wires(server))

    openapi = (
        TestClient(server.http_app())
        .get("/api/openapi.json", headers={"Authorization": "Bearer synthetic-test-key"})
        .json()
    )
    for name in AUTHORING_TOOLS:
        assert openapi["paths"][f"/api/{name}"]["post"]["description"] == pointer
    assert "description" not in openapi["paths"]["/api/ask_memory"]["post"]

    for name in sorted(AUTHORING_TOOLS):
        with pytest.raises(SystemExit):
            main([name, "--help"])
        assert " ".join(capsys.readouterr().out.split()).count(" ".join(pointer.split())) == 1, name
    with pytest.raises(SystemExit):
        main(["ask_memory", "--help"])
    assert "Semantic authoring rules" not in capsys.readouterr().out
