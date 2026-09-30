"""Every structured result a tool emits must validate against its own declared
`outputSchema`. Schema-validating MCP clients reject the whole response
otherwise ("Structured content does not match the tool's output schema").
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import jsonschema
import pytest
from test_mcp_schema_fidelity import _build_server

from exomem import cli_ops

ASK_MEMORY_CASES = {
    "compact_hits": {"query": "the"},
    "full_hits": {"query": "the", "detail": "full"},
    "empty_degraded": {"query": "zzzqqqxx"},
    "deep_pack": {"query": "cache", "deep": True, "detail": "full"},
    "timings": {"query": "the", "include_timings": True},
    "explain": {"query": "cache", "explain": True, "detail": "full"},
    "unit_hits": {"query": "cache", "result_level": "unit"},
    "mixed_hits": {"query": "cache", "result_level": "mixed", "detail": "full"},
    "vector_degraded": {"query": "cache", "mode": "vector"},
    "recent_recall": {"query": ""},
    # A structured OpError is returned as a result, not raised.
    "operation_error": {"query": "the", "relations": ["nosuchrel"]},
}


@pytest.fixture()
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return _build_server(monkeypatch, tmp_path)


def _validate(schema: dict, structured: dict) -> None:
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(structured))
    best = jsonschema.exceptions.best_match(errors)
    assert not errors, f"{best.message} at {list(best.absolute_path)}"


@pytest.mark.parametrize("case", sorted(ASK_MEMORY_CASES))
def test_ask_memory_structured_content_matches_declared_schema(server, case: str) -> None:
    tool = asyncio.run(server.get_tool("ask_memory"))
    schema = tool.to_mcp_tool().model_dump(mode="json", by_alias=True)["outputSchema"]
    result = asyncio.run(server.call_tool("ask_memory", ASK_MEMORY_CASES[case]))
    assert result.structured_content is not None
    _validate(schema, result.structured_content)
    if case == "operation_error":
        assert result.structured_content["result"]["success"] is False


def test_every_tool_schema_admits_the_failure_envelope(server) -> None:
    """A structured refusal is a normal result on every command, so no tool's
    declared schema may exclude it."""
    envelope = cli_ops.envelope(
        False, error={"code": "X", "message": "m", "remediation": None}
    )
    checked = 0
    for tool in asyncio.run(server.list_tools()):
        schema = tool.to_mcp_tool().model_dump(mode="json", by_alias=True).get("outputSchema")
        if not schema:
            continue
        wrapped = schema.get("x-fastmcp-wrap-result")
        _validate(schema, {"result": envelope} if wrapped else envelope)
        checked += 1
    assert checked >= 2, json.dumps(checked)
