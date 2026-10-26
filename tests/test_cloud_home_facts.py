"""What Exomem Cloud Home reads and sets on a cell, through the cell's own `/mcp`.

Home has no route of its own: it calls the same stateless MCP endpoint as every
assistant, with the bearer the gateway derives. These cases send requests the
way the gateway forwards them: no `initialize` first, the gateway's own
`User-Agent`, and the header that names the authenticated OAuth client.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
from pathlib import Path

import httpx
import pytest
from test_cloud_cell import LEGACY_VERSION, TOKEN, _build_cloud_server, _teardown_activation

from exomem import call_ledger, cloud_cell, command_surface, prominence, server


@pytest.fixture
def cell(vault, monkeypatch, tmp_path):
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "machine.json"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    for key in ("EXOMEM_PROMINENCE", "EXOMEM_SURFACE", "EXOMEM_HOSTED_CELL",
                "EXOMEM_DISABLE_CALL_LEDGER"):
        monkeypatch.delenv(key, raising=False)
    call_ledger.reset_chain_cache()  # an earlier test's failed append is not this cell's
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


async def _call(client, name, arguments, *, oauth_client=None, client_context=None, app_id=None):
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
    if app_id is not None:
        headers[cloud_cell.GATEWAY_APP_ID_HEADER] = app_id
    if client_context is not None:
        headers[cloud_cell.GATEWAY_CONTEXT_HEADER] = client_context
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
        # A name that client detection does not know: the registry's context decides.
        chat_app = await _call(
            client, "configure_memory", {},
            oauth_client="Acme Chat", client_context="conversation",
        )
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

    # The session's first recall carries the vault's due-state block beside the hits.
    released = {hit["path"] for hit in recall["result"]["hits"]}
    assert released, "sanity: the recall returned notes"
    assert before["events"] == []
    assert [(e["client"], e["action"], e["items"]) for e in after["events"]] == [
        ("chatgpt", "saved", 1),  # the server derived the path; the commit reports it
        ("claude-ai", "recalled", len(released)),
    ]
    assert after["note_count"] == before["note_count"] + 1


@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("remember", {"content": "The second build fixed the flaky upload.",
                      "title": "Flaky upload fixed", "status": "draft"}),
        ("capture_source", {"title": "Upload log", "content": "raw log text",
                            "source_kind": "log"}),
        ("episode_memory", {"action": "record", "subject": "upload fix",
                            "summary": "Fixed the flaky upload.", "worked_on": ["upload"],
                            "decided": ["retry twice"]}),
    ],
    ids=["remember", "capture_source", "episode_memory"],
)
def test_a_save_whose_path_the_server_chose_still_reports_its_note(cell, tool, arguments):
    """These writers derive the destination themselves, so the call's arguments
    name no page. Unless the commit reports what it saved, Home shows every such
    save with an unknown count. Each saved page, compiled note or not, is one
    more note in Home's count."""

    async def scenario(client):
        before = await _call(client, "bootstrap", {"section": "activity"})
        await _call(client, tool, arguments, oauth_client="ChatGPT", client_context="conversation")
        return before, await _call(client, "bootstrap", {"section": "activity"})

    before, after = cell(scenario)

    assert [(e["action"], e["items"]) for e in after["events"]] == [("saved", 1)]
    assert after["note_count"] == before["note_count"] + 1


def test_an_edit_that_names_its_page_by_reference_counts_that_page_once(cell, vault):
    """An assistant may address a page by its `exomem://` reference. The commit
    then reports the page's path, which is the same page under another name."""
    from exomem import memory_refs

    async def scenario(client):
        saved = await _call(client, "remember", {
            "content": "The second build fixed the flaky upload.",
            "title": "Flaky upload fixed", "status": "draft",
        }, oauth_client="ChatGPT")
        ref = memory_refs.ref_from_markdown((vault / saved["path"]).read_text(encoding="utf-8"))
        assert ref, "sanity: the saved page has a reference"
        await _call(client, "edit_memory", {
            "path": ref, "why": "name the fixing build",
            "operation": {"kind": "replace_string", "old_string": "second build",
                          "new_string": "second build (1.4.2)"},
        }, oauth_client="claude.ai")
        return await _call(client, "bootstrap", {"section": "activity"})

    activity = cell(scenario)

    assert [(e["client"], e["items"]) for e in activity["events"]] == [
        ("claude-ai", 1), ("chatgpt", 1),
    ]


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="needs POSIX permissions that bind the caller",
)
def test_a_recall_the_ledger_could_not_record_is_never_inside_an_idle_interval(
    cell, tmp_path, monkeypatch
):
    """When the ledger volume refuses writes, a recall still succeeds but leaves no
    row and no marker. Home must not then say "No assistant activity since" a
    time before that recall: not from this server, and not from the server that
    a container restart starts on the same pod-scoped ledger."""
    ledger_dir = tmp_path / "ledger"

    async def scenario(client):
        await _call(client, "bootstrap", {"section": "activity"})
        locked = [ledger_dir / "ledger.jsonl", ledger_dir]
        modes = [path.stat().st_mode for path in locked]
        locked[0].chmod(0o400)
        locked[1].chmod(0o500)
        try:
            recall = await _call(client, "ask_memory", {"query": "project"}, oauth_client="Codex")
        finally:
            for path, mode in zip(locked, modes, strict=True):
                path.chmod(mode)
        recalled_by = dt.datetime.now(dt.UTC)
        assert recall["result"], "sanity: the recall returned notes"
        assert not call_ledger.gap_marker_path().exists(), "sanity: no marker was written"
        first = await _call(client, "bootstrap", {"section": "activity"})
        second = await _call(client, "bootstrap", {"section": "activity"})
        # A container restart: a new process, with nothing in memory, on the same ledger.
        monkeypatch.setattr(call_ledger, "WRITER_STARTED_AT", dt.datetime.now(dt.UTC))
        call_ledger.reset_chain_cache()
        restarted = await _call(client, "bootstrap", {"section": "activity"})
        return recalled_by, first, second, restarted

    recalled_by, first, second, restarted = cell(scenario)

    assert first["error"]["code"] == "ACTIVITY_UNAVAILABLE"
    assert second["events"] == []
    assert second["since"] > recalled_by.isoformat(timespec="milliseconds")
    assert restarted["since"] > recalled_by.isoformat(timespec="milliseconds")


def _probe(monkeypatch, *, cloud: bool, headers: dict[str, str]) -> dict:
    """What one real HTTP tool call sees as its caller and its applied context."""
    if cloud:
        monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    else:
        monkeypatch.delenv("EXOMEM_CLOUD_CELL", raising=False)
    monkeypatch.delenv("EXOMEM_SURFACE", raising=False)
    mcp = server.ExomemFastMCP("caller-probe")

    @mcp.tool(name="caller_probe")
    def caller_probe() -> dict:
        with prominence.request_scope(Path("/nonexistent")):
            return {
                "client_name": command_surface.mcp_caller_identity()["client_name"],
                "app_id": command_surface.mcp_caller_identity().get("app_id"),
                "context": prominence.resolved()["context"],
            }

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
                        **headers,
                    },
                    json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "caller_probe", "arguments": {}}},
                )

    response = asyncio.run(scenario())
    assert response.status_code == 200, response.text
    return response.json()["result"]["structuredContent"]


@pytest.mark.parametrize("cloud,named", [(True, "claude.ai"), (False, "node")])
def test_the_gateway_client_header_names_the_caller_only_on_a_cloud_cell(
    monkeypatch, cloud, named
):
    """Only on a cloud cell does the bearer prove the gateway set the header.
    Elsewhere any HTTP caller could send it, so the `User-Agent` still decides."""
    seen = _probe(monkeypatch, cloud=cloud, headers={cloud_cell.GATEWAY_CLIENT_HEADER: "claude.ai"})

    assert seen["client_name"] == named


@pytest.mark.parametrize(
    "cloud,declared,name,context",
    [
        # The registry decides for a client name detection does not know ...
        (True, "conversation", "Acme Chat", "conversation"),
        # ... and against one it does: a loopback-only client is coding.
        (True, "coding", "claude.ai", "coding"),
        # A value outside the closed set is ignored; name detection answers.
        (True, "Conversation", "claude.ai", "conversation"),
        (True, "chat", "Acme Chat", "coding"),
        # Nothing vouches for the header on a local server.
        (False, "conversation", "Acme Chat", "coding"),
    ],
)
def test_on_a_cloud_cell_the_registry_context_decides_which_level_applies(
    monkeypatch, cloud, declared, name, context
):
    """Home saves a chat app's level to the context the control plane's client
    registry assigns it. If the cell applied a different one, the saved choice
    would silently never reach that app."""
    seen = _probe(monkeypatch, cloud=cloud, headers={
        cloud_cell.GATEWAY_CLIENT_HEADER: name,
        cloud_cell.GATEWAY_CONTEXT_HEADER: declared,
    })

    assert seen["context"] == context


@pytest.mark.parametrize("cloud,declared,expected", [
    (True, "12345678-1234-4234-8234-123456789abc", "12345678-1234-4234-8234-123456789abc"),
    (False, "12345678-1234-4234-8234-123456789abc", None),
    (True, "12345678-1234-4234-8234-123456789ABC", None),
    (True, "12345678123442348234123456789abc", None),
    (True, "claimed-client", None),
    (True, "", None),
])
def test_only_a_canonical_cloud_gateway_actor_can_match_a_home_app(
    monkeypatch, cloud, declared, expected
):
    """Malformed IDs and local callers must never attribute activity to a Home app."""
    seen = _probe(monkeypatch, cloud=cloud, headers={cloud_cell.GATEWAY_APP_ID_HEADER: declared})

    assert seen["app_id"] == expected


def test_home_keeps_two_grants_of_one_client_family_separate(cell):
    """An app's feedback must count its own saves, not another grant's same-family saves."""
    first_id = "12345678-1234-4234-8234-123456789abc"
    second_id = "87654321-4321-4321-8321-cba987654321"

    async def scenario(client):
        for app_id, title in ((first_id, "First save"), (second_id, "Second save"),
                              (first_id, "Third save"), (None, "Unresolved save")):
            await _call(client, "remember", {
                "content": "The upload completed.", "title": title, "status": "draft",
            }, oauth_client="ChatGPT", app_id=app_id)
        return await _call(client, "bootstrap", {"section": "activity"})

    after = cell(scenario)

    assert [(e.get("app_id"), e["client"], e["action"], e["items"]) for e in after["events"]] == [
        (None, "chatgpt", "saved", 1),
        (first_id, "chatgpt", "saved", 2),
        (second_id, "chatgpt", "saved", 1),
    ]
    rows = [json.loads(line) for line in call_ledger.ledger_path().read_text().splitlines()]
    assert [row.get("app_id") for row in rows if row["tool"] == "remember"] == [
        first_id, second_id, first_id, None,
    ]
    assert call_ledger.verify() == []
