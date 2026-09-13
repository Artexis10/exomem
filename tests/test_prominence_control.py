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
    assert not commands.invocation_is_read_only(command, {"action": "clear"})
    assert not {"principal", "audience", "vault", "path"} & {p.name for p in command.params}
    context_param = next(p for p in command.params if p.name == "context")
    assert context_param.choices == ("coding", "conversation")
    assert not context_param.required


@pytest.mark.parametrize("level", prominence.CANON)
def test_saved_level_reaches_a_new_bootstrap_and_workflow_projection(vault, level):
    caller = RequestPrincipal("principal:person-a", surface="mcp")
    with request_scope(caller):
        before = _invoke(vault)
        saved = _invoke(vault, action="set", prominence=level, expected_revision=before["revision"])
        assert "engagement" in saved, saved
        assert saved["engagement"]["level"] == level
        assert saved["engagement"]["envelope"]["level"] == level
        assert saved["engagement"]["change_with"].startswith("configure_memory:")
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
    "client,surface,context",
    [
        ("ChatGPT", "chatgpt", "conversation"),
        ("claude.ai", "claude-ai", "conversation"),
        ("codex-mcp-client", "codex", "coding"),
        ("Claude-Code", "claude-code", "coding"),
        ("unknown-client", None, "coding"),
        ("", None, "coding"),
    ],
)
def test_known_request_clients_get_their_surface_default(
    monkeypatch, vault, client, surface, context
):
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
    assert result["context"] == context
    assert result["level"] == ("maximal" if surface in {"chatgpt", "claude-ai"} else "balanced")


@pytest.mark.parametrize(
    "client,context",
    [
        ("codex-mcp-client", "coding"),
        ("Claude-Code", "coding"),
        ("unknown-client", "coding"),
        ("ChatGPT", "conversation"),
        ("claude.ai", "conversation"),
    ],
)
def test_a_saved_context_value_applies_to_the_clients_of_that_context(
    monkeypatch, vault, client, context
):
    """One saved value per context, and the client name alone decides which applies."""
    caller = RequestPrincipal("principal:person-a", surface="mcp")
    with request_scope(caller):
        before = _invoke(vault)
        _invoke(
            vault,
            action="set",
            prominence="off",
            expected_revision=before["revision"],
            context=context,
        )
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
        assert result["context"] == context
        assert result["level"] == "off"
        assert result["source"] == "preference:context"


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


# --------------------------------------------- per-context set and clear, end to end


def test_inspect_reports_the_saved_map_and_the_request_context(vault):
    with request_scope(RequestPrincipal("principal:person-a", surface="mcp")):
        before = _invoke(vault)
        _invoke(
            vault,
            action="set",
            prominence="off",
            expected_revision=before["revision"],
            context="coding",
        )

        inspected = _invoke(vault)

    assert inspected["action"] == "inspect"
    assert inspected["scope"] == "principal-and-vault"
    assert inspected["stored"] is None
    assert inspected["contexts"] == {"coding": "off"}
    assert inspected["context"] == "coding"
    assert inspected["engagement"]["level"] == "off"
    assert inspected["engagement"]["source"] == "preference:context"


def test_the_three_argument_set_still_writes_only_the_identity_wide_value(vault):
    """The 0.81.0 call shape keeps its exact meaning."""
    with request_scope(RequestPrincipal("principal:person-a", surface="mcp")):
        before = _invoke(vault)
        saved = _invoke(
            vault, action="set", prominence="light", expected_revision=before["revision"]
        )

    assert saved["stored"] == "light"
    assert saved["contexts"] == {}


def test_clear_through_the_registry_removes_only_that_context(vault):
    with request_scope(RequestPrincipal("principal:person-a", surface="mcp")):
        before = _invoke(vault)
        identity_wide = _invoke(
            vault, action="set", prominence="maximal", expected_revision=before["revision"]
        )
        saved = _invoke(
            vault,
            action="set",
            prominence="off",
            expected_revision=identity_wide["revision"],
            context="conversation",
        )
        cleared = _invoke(
            vault, action="clear", context="conversation", expected_revision=saved["revision"]
        )
        absent = _invoke(
            vault, action="clear", context="conversation", expected_revision=cleared["revision"]
        )

    assert cleared["mutated"] is True
    assert cleared["contexts"] == {}
    assert cleared["stored"] == "maximal"
    assert absent["mutated"] is False
    assert absent["revision"] == cleared["revision"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "inspect", "prominence": "light"},
        {"action": "inspect", "expected_revision": "missing"},
        {"action": "inspect", "context": "coding"},
        {"action": "set"},
        {"action": "set", "prominence": "light"},
        {"action": "set", "expected_revision": "missing"},
        {"action": "set", "prominence": "light", "context": "coding"},
        {"action": "clear"},
        {"action": "clear", "context": "coding"},
        {"action": "clear", "expected_revision": "missing"},
        {"action": "clear", "context": "coding", "expected_revision": "missing",
         "prominence": "light"},
    ],
)
def test_invalid_argument_combinations_are_refused_without_writing(vault, kwargs):
    from exomem.cli_ops import OpError

    with request_scope(RequestPrincipal("principal:person-a", surface="mcp")):
        with pytest.raises(OpError) as failure:
            _invoke(vault, **kwargs)
        assert failure.value.code == "INVALID_PREFERENCE_ARGUMENTS"
        assert _invoke(vault)["revision"] == "missing"


def test_an_unregistered_action_is_refused_before_the_leaf_runs(vault):
    """Selector coverage is default-deny: an action nobody classified never executes."""
    from exomem.cli_ops import OpError

    with request_scope(RequestPrincipal("principal:person-a", surface="mcp")):
        with pytest.raises(OpError) as failure:
            _invoke(vault, action="wipe", expected_revision="missing")
        assert failure.value.code == "RECEIPT_OUTCOME_MISSING"
        assert _invoke(vault)["revision"] == "missing"


@pytest.mark.parametrize("context", ["", "CODING", "project"])
def test_an_unknown_context_is_an_input_error_without_writing(vault, context):
    from exomem.cli_ops import OpError

    with request_scope(RequestPrincipal("principal:person-a", surface="mcp")):
        with pytest.raises(OpError) as failure:
            _invoke(
                vault,
                action="set",
                prominence="light",
                expected_revision="missing",
                context=context,
            )
        assert failure.value.code in {"INVALID_PREFERENCE_ARGUMENTS", "PREFERENCE_INVALID"}
        assert _invoke(vault)["revision"] == "missing"


def test_the_operator_override_refuses_a_context_set_and_a_clear(vault, monkeypatch):
    from exomem.cli_ops import OpError

    with request_scope(RequestPrincipal("principal:person-a", surface="mcp")):
        before = _invoke(vault)
        saved = _invoke(
            vault,
            action="set",
            prominence="off",
            expected_revision=before["revision"],
            context="coding",
        )
        monkeypatch.setenv("EXOMEM_PROMINENCE", "light")
        with pytest.raises(OpError) as on_set:
            _invoke(
                vault,
                action="set",
                prominence="maximal",
                expected_revision=saved["revision"],
                context="coding",
            )
        with pytest.raises(OpError) as on_clear:
            _invoke(
                vault, action="clear", context="coding", expected_revision=saved["revision"]
            )
        assert on_set.value.code == "PREFERENCE_OPERATOR_OVERRIDE"
        assert on_clear.value.code == "PREFERENCE_OPERATOR_OVERRIDE"
        monkeypatch.delenv("EXOMEM_PROMINENCE")
        assert _invoke(vault)["contexts"] == {"coding": "off"}


def test_rest_context_set_and_cli_clear_share_the_local_owner_preference(
    vault, monkeypatch, capsys
):
    from test_bootstrap import _client

    from exomem.__main__ import main

    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="test-context-key")
    headers = {"Authorization": "Bearer test-context-key"}
    before = client.post("/api/configure_memory", json={}, headers=headers)
    assert before.status_code == 200, before.text
    saved = client.post(
        "/api/configure_memory",
        headers=headers,
        json={
            "action": "set",
            "prominence": "off",
            "expected_revision": before.json()["data"]["revision"],
            "context": "conversation",
        },
    )
    assert saved.status_code == 200, saved.text
    receipt = saved.json()["data"]
    assert receipt["terminal"] and receipt["state"] == "committed"
    assert receipt["contexts"] == {"conversation": "off"}
    assert receipt["context"] in prominence.CONTEXTS
    stale = client.post(
        "/api/configure_memory",
        headers=headers,
        json={"action": "clear", "context": "conversation", "expected_revision": "missing"},
    )
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "PREFERENCE_CONFLICT"

    assert (
        main(
            [
                "configure_memory",
                "--action",
                "clear",
                "--context",
                "conversation",
                "--expected-revision",
                receipt["revision"],
                "--json",
            ]
        )
        == 0
    )
    cleared = json.loads(capsys.readouterr().out)["data"]
    assert cleared["contexts"] == {}
    assert main(["configure_memory", "--action", "inspect", "--json"]) == 0
    inspected = json.loads(capsys.readouterr().out)["data"]
    assert inspected["contexts"] == {}


def test_two_clients_of_one_identity_split_by_engagement_context(vault, monkeypatch):
    """The capability in one sentence: Balanced while coding, Maximal in chat."""
    from fastmcp import Client

    from exomem import server

    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(vault))
    presenting = {"client_name": "Claude-Code"}
    monkeypatch.setattr(
        "exomem.command_surface.mcp_caller_identity",
        lambda: {
            "client_name": presenting["client_name"],
            "transport": "http",
            "client_version": None,
            "session_id": None,
        },
    )
    mcp = server.build_server(require_auth=False)

    async def scenario():
        async with Client(mcp) as coding:
            inspected = await coding.call_tool("configure_memory", {"action": "inspect"})
            saved = await coding.call_tool(
                "configure_memory",
                {
                    "action": "set",
                    "prominence": "balanced",
                    "expected_revision": inspected.data["revision"],
                    "context": "coding",
                },
            )
            assert saved.data["context"] == "coding"
            assert saved.data["contexts"] == {"coding": "balanced"}
        async with Client(mcp) as another_coding_client:
            result = await another_coding_client.call_tool("bootstrap", {"profile": "compact"})
            assert result.data["engagement"]["level"] == "balanced"
            assert result.data["engagement"]["context"] == "coding"
            assert result.data["engagement"]["source"] == "preference:context"
        presenting["client_name"] = "claude.ai"
        async with Client(mcp) as conversational:
            result = await conversational.call_tool("bootstrap", {"profile": "compact"})
            assert result.data["engagement"]["context"] == "conversation"
            assert result.data["engagement"]["level"] == "maximal"
            assert result.data["engagement"]["source"] == "default"

    asyncio.run(scenario())


def test_the_client_name_never_widens_authority(vault, monkeypatch):
    """Context tunes eagerness. Ceilings are product law and do not move with it."""
    from exomem import envelope as envelope_module

    def present(client):
        monkeypatch.setattr(
            "exomem.command_surface.mcp_caller_identity",
            lambda: {
                "client_name": client,
                "transport": "http",
                "client_version": None,
                "session_id": None,
            },
        )

    caller = RequestPrincipal("principal:person-a", surface="mcp")
    with request_scope(caller):
        before = _invoke(vault)
        identity_wide = _invoke(
            vault, action="set", prominence="off", expected_revision=before["revision"]
        )
        _invoke(
            vault,
            action="set",
            prominence="maximal",
            expected_revision=identity_wide["revision"],
            context="coding",
        )
        seen = {}
        for client in ("Claude-Code", "codex-mcp-client", "claude.ai", "ChatGPT", "nobody-knows"):
            present(client)
            with prominence.request_scope(vault):
                engagement = prominence.resolved()
                served = envelope_module.resolved(level=engagement["level"])
            seen[client] = (
                engagement["level"],
                json.dumps(
                    {name: entry["ceiling"] for name, entry in served["classes"].items()},
                    sort_keys=True,
                ),
                json.dumps(served["confirm_required"], sort_keys=True),
            )

    assert {value[0] for value in seen.values()} == {"maximal", "off"}, seen
    assert len({value[1] for value in seen.values()}) == 1, "a ceiling moved with the client name"
    assert len({value[2] for value in seen.values()}) == 1


def test_the_context_never_selects_the_stored_record(vault):
    """Both contexts live in one record under one identity — no second storage path."""
    from exomem import prominence_preferences

    caller = RequestPrincipal("principal:person-a", surface="mcp")
    with request_scope(caller):
        first = _invoke(
            vault, action="set", prominence="off", expected_revision="missing", context="coding"
        )
        _invoke(
            vault,
            action="set",
            prominence="maximal",
            expected_revision=first["revision"],
            context="conversation",
        )
        directory = prominence_preferences._preference_path(
            vault, caller.audience_id
        ).parent
        records = sorted(path.name for path in directory.glob("*.json"))

    assert len(records) == 1, records


@pytest.mark.parametrize(
    "surface,context", [("hosted", "conversation"), ("chatgpt", "conversation"), ("codex", "coding")]
)
def test_the_operator_surface_override_decides_the_context(vault, monkeypatch, surface, context):
    """`EXOMEM_SURFACE` is authoritative over the client's own name."""
    caller = RequestPrincipal("principal:person-a", surface="mcp")
    monkeypatch.setattr(
        "exomem.command_surface.mcp_caller_identity",
        lambda: {
            "client_name": "Claude-Code",
            "transport": "http",
            "client_version": None,
            "session_id": None,
        },
    )
    with request_scope(caller):
        before = _invoke(vault)
        _invoke(
            vault,
            action="set",
            prominence="light",
            expected_revision=before["revision"],
            context=context,
        )
        monkeypatch.setenv("EXOMEM_SURFACE", surface)
        with prominence.request_scope(vault):
            result = prominence.resolved()

    assert result["surface"] == surface
    assert result["context"] == context
    assert result["level"] == "light"
    assert result["source"] == "preference:context"
