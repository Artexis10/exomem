"""An agent's persisted engagement choice survives transport and request boundaries."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from exomem import commands, prominence, writer_lease
from exomem.governance.principal import RequestPrincipal, request_scope


@pytest.fixture
def vault(tmp_path, monkeypatch):
    from exomem.init import init_vault

    vault = tmp_path / "cell"
    init_vault(vault)
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "machine.json"))
    for key in ("EXOMEM_PROMINENCE", "EXOMEM_SURFACE", "EXOMEM_HOSTED_CELL"):
        monkeypatch.delenv(key, raising=False)
    return vault


def _command():
    found = next((c for c in commands.PRODUCT_COMMANDS if c.name == "configure_memory"), None)
    assert found is not None, "the normal agent surface needs a persisted preference control"
    return found


def _invoke(vault: Path, **kwargs):
    return writer_lease.invoke_command(_command(), vault, **kwargs)


def test_configuration_is_registered_with_read_and_write_classification():
    command = _command()
    assert set(command.surfaces) == {"mcp", "rest", "cli"}
    assert commands.invocation_is_read_only(command, {})
    assert commands.invocation_is_read_only(command, {"action": "inspect"})
    assert not commands.invocation_is_read_only(command, {"action": "set"})
    assert not {"principal", "audience", "vault", "path"} & {p.name for p in command.params}


@pytest.mark.parametrize("level", prominence.CANON)
def test_saved_level_reaches_a_new_bootstrap_and_workflow_projection(vault, level):
    caller = RequestPrincipal("principal:person-a", surface="mcp")
    with request_scope(caller):
        before = _invoke(vault)
        saved = _invoke(vault, action="set", prominence=level, expected_revision=before["revision"])
        assert "engagement" in saved, saved
        assert saved["engagement"]["level"] == level
        assert saved["engagement"]["envelope"]["level"] == level
        assert saved["engagement"]["change_with"]["tool"] == "configure_memory"
        bootstrap_command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "bootstrap")
        result = writer_lease.invoke_command(bootstrap_command, vault, profile="compact")
        assert result["engagement"]["level"] == level
        assert result["engagement"]["source"] == "preference"
        assert result["engagement"]["envelope"]["level"] == level
        schema_command = next(c for c in commands.PRODUCT_COMMANDS if c.name == "schema_memory")
        workflow = writer_lease.invoke_command(
            schema_command,
            vault,
            subject="workflow-contracts",
            operation="resolve",
            context={
                "project": None,
                "domain": None,
                "activity": None,
            },
        )
        assert "active_prominence" in workflow, workflow
        assert workflow["active_prominence"] == level
        assert workflow["effective_capture"]["observed_outcomes"]["proactive_permitted"] == (
            level in {"balanced", "maximal"}
        )
        with prominence.request_scope(vault):
            assert prominence.resolve() == level
            assert prominence.capture_gate()["observed_outcomes"]["proactive_permitted"] == (
                level in {"balanced", "maximal"}
            )
    with request_scope(RequestPrincipal("principal:person-b", surface="mcp")):
        assert _invoke(vault)["engagement"]["level"] == "balanced"


@pytest.mark.parametrize(
    "client,surface",
    [
        ("ChatGPT", "chatgpt"),
        ("claude.ai", "claude-ai"),
        ("codex-mcp-client", "codex"),
        ("Claude-Code", "claude-code"),
        ("unknown-client", None),
    ],
)
def test_known_request_clients_get_their_surface_default(monkeypatch, vault, client, surface):
    monkeypatch.setattr(
        "exomem.command_surface.mcp_caller_identity",
        lambda: {
            "client_name": client,
            "transport": "http",
            "client_version": None,
            "session_id": None,
        },
    )
    with prominence.request_scope(vault):
        result = prominence.resolved()
    assert result["surface"] == surface
    assert result["level"] == ("maximal" if surface in {"chatgpt", "claude-ai"} else "balanced")


def test_request_preference_does_not_leak_after_scope_exit(vault):
    with request_scope(RequestPrincipal("principal:person-a", surface="mcp")):
        before = _invoke(vault)
        _invoke(vault, action="set", prominence="maximal", expected_revision=before["revision"])
        with prominence.request_scope(vault):
            assert prominence.resolve() == "maximal"
        assert prominence.resolve() == "balanced"


def test_rest_set_and_cli_inspect_share_the_local_owner_preference(vault, monkeypatch, capsys):
    from test_bootstrap import _client

    from exomem.__main__ import main

    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="test-preference-key")
    headers = {"Authorization": "Bearer test-preference-key"}
    denied = client.post("/api/configure_memory", json={"action": "inspect"})
    assert denied.status_code == 401
    before = client.post("/api/configure_memory", json={}, headers=headers)
    assert before.status_code == 200, before.text
    saved = client.post(
        "/api/configure_memory",
        headers=headers,
        json={
            "action": "set",
            "prominence": "maximal",
            "expected_revision": before.json()["data"]["revision"],
        },
    )
    assert saved.status_code == 200, saved.text
    receipt = saved.json()["data"]
    assert receipt["terminal"] and receipt["state"] == "committed"
    assert receipt["receipt_id"]
    assert receipt["engagement"]["level"] == "maximal"
    stale = client.post(
        "/api/configure_memory",
        headers=headers,
        json={
            "action": "set",
            "prominence": "off",
            "expected_revision": "missing",
        },
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "PREFERENCE_CONFLICT"
    assert main(["configure_memory", "--action", "inspect", "--json"]) == 0
    inspected = json.loads(capsys.readouterr().out)["data"]
    assert inspected["revision"] == receipt["revision"]
    assert inspected["engagement"]["level"] == "maximal"


@pytest.mark.parametrize("level", prominence.CANON)
def test_mcp_setting_is_visible_to_a_new_client(vault, monkeypatch, level):
    from fastmcp import Client

    from exomem import server

    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    mcp = server.build_server(require_auth=False)

    async def scenario():
        async with Client(mcp) as first:
            inspected = await first.call_tool("configure_memory", {"action": "inspect"})
            before = inspected.data
            saved = await first.call_tool(
                "configure_memory",
                {
                    "action": "set",
                    "prominence": level,
                    "expected_revision": before["revision"],
                },
            )
            assert saved.data["engagement"]["level"] == level
        async with Client(mcp) as second:
            result = await second.call_tool("bootstrap", {"profile": "compact"})
            assert result.data["engagement"]["level"] == level

    asyncio.run(scenario())
