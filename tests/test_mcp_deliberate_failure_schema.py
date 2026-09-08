from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TypedDict

import pytest
from fastmcp import Client, FastMCP

from exomem import command_surface, writer_lease
from exomem.cli_ops import OpError
from exomem.governance import principal as principal_module
from exomem.retrieval_models import FindEnvelope, RetrievalHit


class Detail(TypedDict):
    count: int


class ObjectResult(TypedDict):
    detail: Detail


def test_typed_recall_schema_accepts_success_and_deliberate_failure(
    tmp_path: Path, monkeypatch
) -> None:
    def recall_leaf(vault: Path) -> list[RetrievalHit] | FindEnvelope:
        raise AssertionError(f"invoke seam should replace leaf for {vault}")

    command = command_surface.Command(
        name="ask_memory",
        leaf=recall_leaf,
        params=(),
        surfaces=frozenset({"mcp"}),
    )
    bound = command_surface.bind_vault(
        recall_leaf,
        tmp_path,
        name="ask_memory",
        command=command,
    )
    server = FastMCP("typed-deliberate-failure")
    command_surface.register_mcp_tool(server, bound)
    refusal = OpError(
        "RETRIEVAL_INDEX_WARMING",
        "semantic retrieval index is still warming",
        details={
            "status": "retryable",
            "complete": False,
            "committed": False,
            "retry_after_ms": 750,
            "site": "semantic_unit_catalog",
            "waited_ms": 5000,
            "request_id": "11111111-1111-4111-8111-111111111111",
            "receipt_id": None,
        },
    )
    error_payload = {**refusal.as_public_dict(), "future_public_detail": {"nested": [1, 2]}}
    monkeypatch.setattr(refusal, "as_public_dict", lambda: error_payload)
    hit: RetrievalHit = {
        "path": "Knowledge Base/Notes/food-bank.md",
        "type": "insight",
        "scope": "kb",
        "title": "Food bank volunteering",
        "updated": "2026-09-08",
    }
    outcomes = iter([refusal, [hit], RuntimeError("unexpected recall fault")])

    def invoke(*_args, **_kwargs):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(writer_lease, "invoke_command", invoke)

    async def exercise() -> None:
        owner = principal_module.owner_principal(surface="mcp")
        with principal_module.request_scope(owner):
            async with Client(server) as client:
                failed = await client.call_tool_mcp("ask_memory", {})
                assert failed.isError is False
                failed_payload = failed.structuredContent["result"]
                assert failed_payload["success"] is False
                assert failed_payload["error"] == refusal.as_public_dict()

                succeeded = await client.call_tool_mcp("ask_memory", {})
                assert succeeded.isError is False
                assert succeeded.structuredContent == {"result": [hit]}

                unexpected = await client.call_tool_mcp("ask_memory", {})
                assert unexpected.isError is True
                assert unexpected.structuredContent is None
                assert "unexpected recall fault" in unexpected.content[0].text

    asyncio.run(exercise())


@pytest.mark.parametrize("typed", [False, True])
def test_direct_object_success_shape_and_open_failure_details_are_preserved(tmp_path, monkeypatch, typed):
    def typed_leaf(vault: Path) -> ObjectResult:
        return {"detail": {"count": 3}}

    def plain_leaf(vault: Path) -> dict:
        return {"detail": {"count": 3}, "additional_success_field": "kept"}

    leaf = typed_leaf if typed else plain_leaf
    command = command_surface.Command(name="inspect", leaf=leaf, params=(), surfaces=frozenset({"mcp"}))
    bound = command_surface.bind_vault(leaf, tmp_path, name="inspect", command=command)
    server = FastMCP("object-result-shapes")
    tool = command_surface.register_mcp_tool(server, bound)
    assert not tool.output_schema.get("x-fastmcp-wrap-result")
    expected_success = leaf(tmp_path)
    refusal = OpError("MUTATION_BUSY", "busy", remediation="retry")
    expected_error = {**refusal.as_public_dict(), "future_public_detail": {"nested": [1, 2]}}
    monkeypatch.setattr(refusal, "as_public_dict", lambda: expected_error)
    outcomes = iter([expected_success, refusal])

    def invoke(*_args, **_kwargs):
        value = next(outcomes)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(writer_lease, "invoke_command", invoke)

    async def exercise():
        with principal_module.request_scope(principal_module.owner_principal(surface="mcp")):
            async with Client(server) as client:
                result = await client.call_tool_mcp("inspect", {})
                assert result.structuredContent == expected_success
                assert result.isError is False
                failed = await client.call_tool_mcp("inspect", {})
                assert failed.structuredContent == {"success": False, "error": expected_error}
                assert failed.isError is False

    asyncio.run(exercise())
