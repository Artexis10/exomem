"""What Exomem Cloud Home reads and sets on a cell, through the cell's own `/mcp`.

Home has no route of its own: it calls the same stateless MCP endpoint as every
assistant, with the bearer the gateway derives. These cases send requests the
way the gateway forwards them: no `initialize` first, the gateway's own
`User-Agent`, and the header that names the authenticated OAuth client.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from test_cloud_cell import LEGACY_VERSION, TOKEN, _build_cloud_server, _teardown_activation

from exomem import cloud_cell, command_surface, server


@pytest.fixture
def cell(vault, monkeypatch, tmp_path):
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "machine.json"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    for key in ("EXOMEM_PROMINENCE", "EXOMEM_SURFACE", "EXOMEM_HOSTED_CELL",
                "EXOMEM_DISABLE_CALL_LEDGER"):
        monkeypatch.delenv(key, raising=False)
    mcp, _ = _build_cloud_server(monkeypatch)
    app = mcp.http_app(stateless_http=True, json_response=True)

    def run(scenario):
        async def wrapped():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://cell.local"
                ) as client:
                    return await scenario(client)

        return asyncio.run(wrapped())

    yield run
    _teardown_activation(mcp)


async def _call(client, name, arguments, *, oauth_client=None):
    """One tools/call exactly as the gateway forwards it."""
    headers = {
        "accept": "application/json, text/event-stream",
        "content-type": "application/json",
        "authorization": f"Bearer {TOKEN}",
        "mcp-protocol-version": LEGACY_VERSION,
        "user-agent": "node",
    }
    if oauth_client is not None:
        headers[cloud_cell.GATEWAY_CLIENT_HEADER] = oauth_client
    response = await client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}},
    )
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result["isError"] is False, result
    return result["structuredContent"]


def test_home_sets_the_chat_apps_level_that_a_chat_app_then_gets(cell):
    """Home is neither a chat app nor a coding client, yet must show and set both.
    The chat app behind the gateway must then actually get what Home set."""

    async def scenario(client):
        shown = await _call(client, "configure_memory", {})
        set_ = await _call(client, "configure_memory", {
            "action": "set", "context": "conversation", "prominence": "light",
            "expected_revision": shown["revision"],
        })
        chat_app = await _call(client, "configure_memory", {}, oauth_client="claude.ai")
        stale = await _call(client, "configure_memory", {
            "action": "set", "context": "conversation", "prominence": "off",
            "expected_revision": shown["revision"],
        })
        cleared = await _call(client, "configure_memory", {
            "action": "clear", "context": "conversation", "expected_revision": set_["revision"],
        })
        return shown, set_, chat_app, stale, cleared

    shown, set_, chat_app, stale, cleared = cell(scenario)

    assert shown["levels"] == {
        "coding": {"level": "balanced", "source": "default"},
        "conversation": {"level": "maximal", "source": "default"},
    }
    assert set_["levels"] == {
        "coding": {"level": "balanced", "source": "default"},
        "conversation": {"level": "light", "source": "preference:context"},
    }
    assert chat_app["context"] == "conversation"
    assert chat_app["engagement"]["level"] == "light"
    # A second writer holding the old revision cannot overwrite Home's choice.
    assert stale["success"] is False
    assert stale["error"]["code"] == "PREFERENCE_CONFLICT"
    assert cleared["levels"]["conversation"] == {"level": "maximal", "source": "default"}


def test_home_sees_a_recall_with_the_notes_it_released_and_the_note_count_move(cell):
    """The activity line must say which assistant recalled how many notes, and a
    save must move the note count even though Home polls a cached count."""

    async def scenario(client):
        before = await _call(client, "bootstrap", {"section": "activity"})
        recall = await _call(client, "ask_memory", {"query": "project"}, oauth_client="claude.ai")
        await _call(client, "remember", {
            "content": "The second build fixed the flaky upload.",
            "title": "Flaky upload fixed", "status": "draft",
        }, oauth_client="ChatGPT")
        after = await _call(client, "bootstrap", {"section": "activity"})
        return before, recall, after

    before, recall, after = cell(scenario)

    released = {hit["path"] for hit in recall["result"]}
    assert released, "sanity: the recall returned notes"
    assert before["events"] == []
    assert [(e["client"], e["action"], e["items"]) for e in after["events"]] == [
        ("chatgpt", "saved", None),  # the server derived the path and reported none
        ("claude-ai", "recalled", len(released)),
    ]
    assert after["note_count"] == before["note_count"] + 1


@pytest.mark.parametrize("cloud,named", [(True, "claude.ai"), (False, "node")])
def test_the_gateway_client_header_names_the_caller_only_on_a_cloud_cell(
    monkeypatch, cloud, named
):
    """Only on a cloud cell does the bearer prove the gateway set the header.
    Elsewhere any HTTP caller could send it, so the `User-Agent` still decides."""
    if cloud:
        monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    else:
        monkeypatch.delenv("EXOMEM_CLOUD_CELL", raising=False)
    mcp = server.ExomemFastMCP("caller-probe")

    @mcp.tool(name="caller_probe")
    def caller_probe() -> dict:
        return command_surface.mcp_caller_identity()

    app = mcp.http_app(stateless_http=True, json_response=True)

    async def scenario():
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://cell.local"
            ) as client:
                return await client.post(
                    "/mcp",
                    headers={
                        "accept": "application/json, text/event-stream",
                        "content-type": "application/json",
                        "mcp-protocol-version": LEGACY_VERSION,
                        "user-agent": "node",
                        cloud_cell.GATEWAY_CLIENT_HEADER: "claude.ai",
                    },
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "caller_probe", "arguments": {}}},
                )

    response = asyncio.run(scenario())

    assert response.status_code == 200, response.text
    assert response.json()["result"]["structuredContent"]["client_name"] == named
