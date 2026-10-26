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
import os
from pathlib import Path

import pytest
from fastmcp.server.dependencies import without_injected_parameters
from fastmcp.tools.function_parsing import get_cached_typeadapter  # noqa: F401
from test_mcp_schema_fidelity import _build_server

REPO_ROOT = Path(__file__).resolve().parents[1]

# Measure tool-object bytes with scripts/measure-tool-schema-bytes.py, excluding RPC framing.
# No token or quality gain is claimed. Each changed ceiling rounds up to 50 bytes.
# The separate bootstrap core budget stays unchanged.
TOTAL_BUDGET = 160_000

#: Per-tool ceilings for nested jobs and hidden grouped compatibility spellings.
TOOL_CEILINGS: dict[str, int] = {
    "adopt_vault": 1350,
    "adoption_studio": 3800,
    "bootstrap": 1450,
    "browse_memory": 6700,
    "capture_source": 3350,
    "import_memory": 5500,
    "change_connections": 3200,
    "change_files": 7100,
    "change_governance": 4650,
    "change_media": 1200,
    "change_observations": 6200,
    "change_plans": 3350,
    "change_records": 5550,
    "change_schema": 9250,
    "change_session_access": 1700,
    "change_settings": 1500,
    "check_memory": 6600,
    "configure_memory": 1400,
    "connect_memory": 4500,
    "coordination_status": 500,
    "create_note": 5700,
    "edit_note": 7150,
    "episode_memory": 4200,
    "govern_memory": 3100,
    "grant_session_access": 2050,
    "maintain_memory": 3750,
    "manage_authorization_session": 2400,
    "manage_memory_file": 5150,
    "observe_memory": 5050,
    "plan_memory": 2700,
    "prepare_note": 5200,
    "preserve_artifacts": 2300,
    "preserve_evidence": 1350,
    "process_media": 1050,
    "query_dataset": 1550,
    "read_import": 1900,
    "read_connections": 3300,
    "read_episode": 1500,
    "read_governance": 2250,
    "read_media": 1250,
    "read_memory": 3150,
    "read_plans": 2700,
    "read_records": 3300,
    "read_review": 7550,
    "read_schema": 16300,
    "recall_context": 2950,
    "record_episode": 4150,
    "record_memory": 4600,
    "repair_memory": 2000,
    "review_item_context": 1300,
    "review_memory": 3500,
    "schema_memory": 4200,
    "search_memory": 4000,
    "supersede_note": 4850,
    "transfer_artifact": 1300,
    "triage_memory": 4100,
    "viewer_map": 3350,
}

#: A Cloud cell serves the local surface minus `CLOUD_SURFACE_EXCLUSIONS` plus the Cloud-only
#: control-plane tools, so its budget is the local one re-based on exactly those differences.
#: The only new numbers are the ceilings of the two tools that no other surface lists.
CLOUD_ONLY_CEILINGS: dict[str, int] = {
    "observe_transfer": 1600,
    "transfer_memory": 1650,
}


def _cloud_total_budget() -> int:
    from exomem import commands
    from exomem.command_surface import listed_names

    ordinary = {name for command in commands.product_commands_for("mcp")
                for name in listed_names(command, "nested")}
    cloud = {name for command in commands.product_commands_for("mcp", cloud=True)
             for name in listed_names(command, "nested")}
    withheld = sum(TOOL_CEILINGS[name] for name in ordinary - cloud)
    return TOTAL_BUDGET - withheld + sum(CLOUD_ONLY_CEILINGS.values())


# These public jobs carry the source commands' authoring contract once per tool.
AUTHORING_TOOLS = frozenset({"create_note", "supersede_note", "edit_note", "change_observations", "change_files", "browse_memory", "prepare_note"})


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
    assert sum(TOOL_CEILINGS[row["name"]] for row in rows) <= TOTAL_BUDGET


def test_cloud_surface_stays_under_its_budget_and_ceilings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Cloud tool list is what a hosted client resends each turn; the local gates above
    never build it, so a Cloud-only tool could grow unmeasured."""
    rows = _measure_module().measure(_wires(_build_server(monkeypatch, tmp_path, cloud=True)))
    ceilings = {**TOOL_CEILINGS, **CLOUD_ONLY_CEILINGS}
    total = sum(row["total"] for row in rows)
    assert total <= _cloud_total_budget(), f"Cloud surface is {total:,} B, budget {_cloud_total_budget():,} B"
    unbudgeted = sorted(row["name"] for row in rows if row["name"] not in ceilings)
    assert not unbudgeted, f"Cloud tools without a ceiling: {unbudgeted}"
    over = [f"{r['name']}: {r['total']:,} > {ceilings[r['name']]:,}" for r in rows if r["total"] > ceilings[r["name"]]]
    assert not over, "over ceiling: " + "; ".join(over)


def test_semantic_authoring_contract_is_carried_once_per_authoring_tool(server) -> None:
    from exomem.semantic_authoring import AUTHORING_CONTRACT, contract_identity

    marker = f"Semantic authoring [{contract_identity()}]"
    for wire in _wires(server):
        in_description = wire["description"].count(marker)
        in_parameters = str(wire["inputSchema"]).count(marker)
        expected = 1 if wire["name"] in AUTHORING_TOOLS else 0
        assert in_description == expected, wire["name"]
        assert in_parameters == 0, f"{wire['name']} repeats the contract in a parameter"
        if wire["name"] in AUTHORING_TOOLS:
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
    assert checked > 0, "the check must reach at least one nullable parameter"


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
    grouped_tools = {"create_note", "supersede_note", "edit_note", "observe_memory", "manage_memory_file"}
    for name in grouped_tools:
        assert openapi["paths"][f"/api/{name}"]["post"]["description"] == pointer
    assert "description" not in openapi["paths"]["/api/search_memory"]["post"]

    for name in sorted(grouped_tools):
        with pytest.raises(SystemExit):
            main([name, "--help"])
        assert " ".join(capsys.readouterr().out.split()).count(" ".join(pointer.split())) == 1, name
    with pytest.raises(SystemExit):
        main(["search_memory", "--help"])
    assert "Semantic authoring rules" not in capsys.readouterr().out


def test_a_schema_capture_leaves_the_callers_state_root_alone() -> None:
    """Run from an operator shell, the inherited state root is the live
    service's, and every capture left a `schema_vault-<digest>` directory in it."""
    caller_root = Path(os.environ["EXOMEM_STATE_ROOT"])

    def entries() -> list[str]:
        return sorted(path.name for path in caller_root.iterdir()) if caller_root.exists() else []

    before = entries()
    assert _measure_module().wire_tools()
    assert entries() == before
