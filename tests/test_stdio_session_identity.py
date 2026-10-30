"""One MCP session identity per stdio connection, through a real stdio server.

`mcp` builds a new connection object for every request on stdio, so FastMCP's own
`session_id` is a fresh value on every call there. These tests run `python -m exomem
--transport stdio` as a subprocess and drive it with a FastMCP client, because the
defect lives in that pairing and an in-process stub cannot reproduce it. A server
process takes several seconds to start, so the tests open as few connections as they can.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

pytestmark = pytest.mark.timeout(240)


@pytest.fixture
def stdio_env(vault: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """The test-isolated environment the server subprocess inherits, with its own call ledger."""
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("EXOMEM_DISABLE_CALL_LEDGER", raising=False)
    return dict(os.environ)


def _ledger_rows(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "ledger" / "ledger.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


async def _connection(env: dict[str, str], work: Callable[[Client], Awaitable[Any]]) -> Any:
    """Open one stdio connection (one server process), run `work` on it, and close it."""
    transport = StdioTransport(
        command=sys.executable, args=["-m", "exomem", "--transport", "stdio"], env=env
    )
    async with Client(transport) as client:
        return await work(client)


def _in_connection(env: dict[str, str], work: Callable[[Client], Awaitable[Any]]) -> Any:
    return asyncio.run(_connection(env, work))


def test_every_call_on_one_stdio_connection_reports_one_session(
    stdio_env: dict[str, str], tmp_path: Path
) -> None:
    async def three_calls(client: Client) -> None:
        for _ in range(3):
            await client.call_tool("bootstrap", {}, raise_on_error=False)

    _in_connection(stdio_env, three_calls)

    rows = _ledger_rows(tmp_path)
    assert [row["transport"] for row in rows] == ["stdio"] * 3
    assert rows[0]["session_id"]
    assert {row["session_id"] for row in rows} == {rows[0]["session_id"]}
    assert {row["caller_principal_hash"] for row in rows} == {f"session:{rows[0]['session_id']}"}


def test_a_notice_one_stdio_connection_created_is_told_to_the_next_connection_not_to_itself(
    stdio_env: dict[str, str], tmp_path: Path
) -> None:
    async def create_then_read(client: Client) -> dict[str, int]:
        operation = {
            "kind": "create-entity",
            "entity_type": "concept",
            "name": "Tidal Ledger",
            "summary": "A synthetic accounting concept used to exercise promotions.",
        }
        for _ in range(60):
            created = await client.call_tool(
                "change_connections", {"operation": operation}, raise_on_error=False
            )
            outcome = (created.structured_content or {}).get("result") or {}
            if (outcome.get("error") or {}).get("code") != "MUTATION_WARMING":
                break
            await asyncio.sleep(1)
        assert outcome.get("ok") is True, outcome
        return await read(client)

    async def read(client: Client) -> dict[str, int]:
        result = await client.call_tool(
            "bootstrap", {"section": "epistemics"}, raise_on_error=False
        )
        block = (result.structured_content or {}).get("due_state") or {}
        return dict(block.get("categories") or {})

    creator = _in_connection(stdio_env, create_then_read)
    second = _in_connection(stdio_env, read)

    assert "recent_promotions" not in creator, "the creating connection never counts its own entry"
    assert second.get("recent_promotions") == 1, "the next connection is told"
    sessions = [row["session_id"] for row in _ledger_rows(tmp_path)]
    assert sessions[0] == sessions[1] != sessions[2], "two connections are two sessions"
