"""The call ledger and `exomem doctor` say which protocol features each MCP client declares.

Every case drives the real MCP HTTP app, so the revision and capabilities are read from
the request the way a client sends them, not from a faked context.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from exomem import call_ledger, doctor, server

LEGACY_VERSION = "2025-11-25"
MODERN_VERSION = "2026-07-28"
TASKS = "io.modelcontextprotocol/tasks"


@pytest.fixture
def ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "logs"
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(directory))
    monkeypatch.delenv("EXOMEM_DISABLE_CALL_LEDGER", raising=False)
    monkeypatch.delenv("EXOMEM_LOG_CONTENT_PRIVATE", raising=False)
    call_ledger.reset_chain_cache()
    yield directory
    call_ledger.reset_chain_cache()


@asynccontextmanager
async def _client(*, stateless: bool) -> AsyncIterator[httpx.AsyncClient]:
    mcp = server.ExomemFastMCP("census")
    mcp.add_middleware(server.CallTraceMiddleware())

    @mcp.tool
    async def probe() -> dict[str, bool]:
        return {"ok": True}

    app = mcp.http_app(stateless_http=stateless, json_response=True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://census.example"
        ) as client:
            yield client


def _headers(user_agent: str, **extra: str) -> dict[str, str]:
    return {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "user-agent": user_agent,
        **extra,
    }


async def _call_modern(
    client: httpx.AsyncClient, *, capabilities: dict[str, Any], client_info: dict[str, str] | None,
    user_agent: str = "test-agent/1",
) -> None:
    meta: dict[str, Any] = {
        "io.modelcontextprotocol/protocolVersion": MODERN_VERSION,
        "io.modelcontextprotocol/clientCapabilities": capabilities,
    }
    if client_info is not None:
        meta["io.modelcontextprotocol/clientInfo"] = client_info
    response = await client.post(
        "/mcp",
        headers=_headers(
            user_agent, accept="application/json", **{
                "mcp-protocol-version": MODERN_VERSION, "mcp-method": "tools/call", "mcp-name": "probe",
            }
        ),
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"_meta": meta, "name": "probe", "arguments": {}}},
    )
    assert response.status_code == 200, response.text


async def _call_legacy(
    client: httpx.AsyncClient, *, user_agent: str, session: str | None = None
) -> None:
    extra = {"mcp-protocol-version": LEGACY_VERSION}
    if session is not None:
        extra["mcp-session-id"] = session
    response = await client.post(
        "/mcp", headers=_headers(user_agent, **extra),
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "probe", "arguments": {}}},
    )
    assert response.status_code == 200, response.text


def _rows(directory: Path) -> list[dict[str, Any]]:
    path = directory / "ledger.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.anyio
async def test_a_2026_07_28_call_records_its_revision_and_the_extensions_it_declares(ledger_dir: Path) -> None:
    async with _client(stateless=True) as client:
        await _call_modern(
            client,
            capabilities={"extensions": {TASKS: {}}, "elicitation": {"form": {}}},
            client_info={"name": "claude-code", "version": "2.9"},
        )

    [row] = _rows(ledger_dir)
    assert row["client_name"] == "claude-code"
    assert row["protocol_version"] == MODERN_VERSION
    assert row["client_extensions"] == ["elicitation:form", TASKS]
    assert call_ledger.verify() == []


@pytest.mark.anyio
async def test_a_stateless_older_call_records_its_revision_and_leaves_the_extensions_unknown(
    ledger_dir: Path,
) -> None:
    async with _client(stateless=True) as client:
        await _call_legacy(client, user_agent="codex/0.5")

    [row] = _rows(ledger_dir)
    assert row["client_name"] == "codex/0.5"
    assert row["protocol_version"] == LEGACY_VERSION
    # A stateless call keeps no handshake, so "declares nothing" would be a claim the server cannot make.
    assert "client_extensions" not in row


@pytest.mark.anyio
async def test_a_stateful_older_call_records_the_extensions_its_handshake_declared(ledger_dir: Path) -> None:
    async with _client(stateless=False) as client:
        initialized = await client.post(
            "/mcp", headers=_headers("local-agent/1"),
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": LEGACY_VERSION,
                "capabilities": {"extensions": {TASKS: {}}},
                "clientInfo": {"name": "stdio-like", "version": "1"},
            }},
        )
        session = initialized.headers["mcp-session-id"]
        await client.post(
            "/mcp",
            headers=_headers("local-agent/1", **{"mcp-session-id": session, "mcp-protocol-version": LEGACY_VERSION}),
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        await _call_legacy(client, user_agent="local-agent/1", session=session)

    [row] = _rows(ledger_dir)
    assert row["client_name"] == "stdio-like"
    assert row["protocol_version"] == LEGACY_VERSION
    assert row["client_extensions"] == [TASKS]


@pytest.mark.anyio
async def test_a_2026_07_28_call_without_client_info_is_named_by_its_user_agent_and_declares_nothing(
    ledger_dir: Path,
) -> None:
    async with _client(stateless=True) as client:
        await _call_modern(client, capabilities={}, client_info=None, user_agent="bare-client/4")

    [row] = _rows(ledger_dir)
    assert row["client_name"] == "bare-client/4"
    assert row["client_extensions"] == []


def test_a_client_cannot_fill_a_row_with_extension_ids(ledger_dir: Path) -> None:
    stuffed = [f"vendor.example/{number:03d}-{'x' * 200}" for number in range(40)]

    row = call_ledger.build_row(
        sequence=1, prev_hash=call_ledger.GENESIS_HASH, request_id="r", tool="probe", outcome="ok",
        duration_ms=1.0, client_extensions=stuffed,
    )

    assert len(row["client_extensions"]) == 16
    assert all(len(name) <= 64 for name in row["client_extensions"])
    assert row["client_extensions_truncated"] == 24
    assert row["row_hash"] == call_ledger.row_hash(row)


@pytest.mark.anyio
async def test_the_doctor_lists_each_client_and_revision_of_the_last_day_with_its_extensions(
    ledger_dir: Path,
) -> None:
    async with _client(stateless=True) as client:
        await _call_modern(
            client, capabilities={"extensions": {TASKS: {}}}, client_info={"name": "claude-code", "version": "2"},
        )
        await _call_legacy(client, user_agent="codex/0.5")
    older = dt.datetime.now(dt.UTC) - dt.timedelta(hours=30)
    stale = call_ledger.build_row(
        sequence=1, prev_hash=call_ledger.GENESIS_HASH, request_id="old", tool="probe", outcome="ok",
        duration_ms=1.0, client_name="yesterday-client", protocol_version=LEGACY_VERSION,
        timestamp=older.isoformat(timespec="milliseconds"),
    )
    path = ledger_dir / "ledger.jsonl"
    path.write_text(json.dumps(stale) + "\n" + path.read_text(encoding="utf-8"), encoding="utf-8")

    check = doctor._check_mcp_clients()

    assert check.status == "pass"
    lines = check.message.splitlines()
    assert lines[0] == "2 tool call(s) in the last 24 hours:"
    assert f"  claude-code on {MODERN_VERSION}: {TASKS} (1 tool call(s))" in lines
    assert f"  codex/0.5 on {LEGACY_VERSION}: declared extensions not recorded (1 tool call(s))" in lines
    assert "yesterday-client" not in check.message


@pytest.mark.anyio
async def test_the_doctor_says_its_census_is_partial_when_the_live_ledger_starts_inside_the_day(
    ledger_dir: Path,
) -> None:
    # A rotation leaves the live ledger starting a few minutes ago: the census must not
    # present those minutes as the whole day.
    async with _client(stateless=True) as client:
        await _call_legacy(client, user_agent="codex/0.5")

    check = doctor._check_mcp_clients()

    assert check.status == "pass"
    first = check.message.splitlines()[0]
    assert first.startswith("1 tool call(s) since ")
    assert first.endswith("UTC (partial: the live ledger starts there):")
    assert check.details["complete"] is False
    assert check.details["covered_since"] is not None
