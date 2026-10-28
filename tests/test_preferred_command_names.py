"""Public spellings share execution identity without duplicating discovery."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from fastmcp import Client

from exomem import cli_ops, commands, product_invoke, server, writer_lease
from exomem.command_surface import resolve_command


def test_name_resolution_preserves_filtered_command_identity() -> None:
    for command in commands.PRODUCT_COMMANDS:
        assert resolve_command(command.public_name, (command,)) is command
        assert resolve_command(command.name, (command,)) is command
        assert resolve_command(command.public_name, ()) is None
        assert resolve_command(command.name, ()) is None
    # 33 commands on every surface plus the 2 that only a Cloud cell lists.
    assert len(commands.PRODUCT_COMMANDS) == len(set(commands.PRODUCT_PUBLIC_NAMES)) == 35
    assert len(commands.product_commands_for("mcp")) == 33
    assert sum(command.public_name != command.name for command in commands.PRODUCT_COMMANDS) == 7


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
@pytest.mark.parametrize("cloud", (False, True))
def test_mcp_discovery_and_both_names_reach_guarded_execution(vault, monkeypatch, cloud) -> None:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(vault.parent / "name-lease"))
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1" if cloud else "0")
    if cloud:
        monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", "name-proof")
        monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN", "a" * 40)
    monkeypatch.delenv("EXOMEM_MCP_LEGACY_COMPAT", raising=False)
    mcp = server.build_server(require_auth=False)

    async def exercise():
        async with Client(mcp) as client:
            discovered = {tool.name for tool in await client.list_tools()}
            for excluded in commands.CLOUD_SURFACE_EXCLUSIONS if cloud else ():
                contributors = [command for command in commands.product_commands_for("mcp", cloud=True)
                                if any(rendering.name == excluded for rendering in command.renderings)]
                if contributors:
                    # A shared job can remain, but none of its variants comes from the excluded command.
                    tool = next(tool for tool in await client.list_tools() if tool.name == excluded)
                    kinds = {branch["properties"]["kind"]["const"]
                             for branch in tool.input_schema["properties"]["operation"]["oneOf"]}
                    assert kinds == {variant.kind for command in contributors for rendering in command.renderings
                                     if rendering.name == excluded for variant in rendering.variants}
                else:
                    assert excluded not in discovered
                    assert await mcp.get_tool(excluded) is None
            for command in commands.product_commands_for("mcp", cloud=cloud):
                if command.renderings:
                    # Discovered as its renderings; the grouped name stays callable but unlisted.
                    assert {r.name for r in command.renderings} <= discovered
                    assert command.name not in discovered or command.name in {r.name for r in command.renderings}
                    assert await mcp.get_tool(command.name) is not None
                    continue
                assert command.public_name in discovered
                if command.public_name != command.name:
                    assert command.name not in discovered
                    compatible = await mcp.get_tool(command.name)
                    preferred = await mcp.get_tool(command.public_name)
                    assert compatible.parameters == preferred.parameters
            for name in ("ask_memory", "search_memory"):
                result = await client.call_tool(name, {"query": "metabolism", "mode": "keyword"})
                assert result.data
            for name in ("activate_context", "recall_context"):
                result = await client.call_tool(name, {"turn": ""})
                assert result.data["abstained"] is True
            for name in ("compile_source", "prepare_note"):
                result = await client.call_tool(name, {"sources": ["Knowledge Base/Sources/Books/2026-05-03-elimination-diet.md"]})
                assert result.data
            for name in ("remember", "create_note", "replace_memory", "supersede_note"):
                arguments = {
                    "title": "Public spelling proof",
                    "content": "## Observations\n\n- [constraint] Keep mutation receipts stable.\n",
                    "validate_only": True,
                }
                if resolve_command(name, commands.PRODUCT_COMMANDS).name == "replace_memory":
                    arguments["old_path"] = "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md"
                result = await client.call_tool(name, arguments)
                assert result.data["mutated"] is False
                assert result.data["draft_id"]
            for name in ("edit_memory", "edit_note"):
                result = await client.call_tool(name, {"path": "missing.md", "operation": {"kind": "replace_string", "old_string": "old", "new_string": "new"}}, raise_on_error=False)
                assert result.is_error
                assert "Unknown tool" not in str(result.content)

    try:
        asyncio.run(exercise())
    finally:
        writer_lease.reset_managers_for_tests()


def test_cloud_trace_classifies_preferred_writes() -> None:
    from exomem import cloud_plugin_evals

    assert cloud_plugin_evals._is_write({"name": "create_note", "arguments": {}})
    assert cloud_plugin_evals._is_write({"name": "edit_note", "arguments": {}})
    assert not cloud_plugin_evals._is_write({"name": "search_memory", "arguments": {}})


def test_cli_lookup_uses_filtered_registry(monkeypatch) -> None:
    original = product_invoke.product_command("remember")
    assert product_invoke.product_command("create_note") is original
    filtered = replace(original, tier=2)
    monkeypatch.setattr(commands, "PRODUCT_COMMANDS", (filtered,))
    for name in filtered.callable_names:
        with pytest.raises(cli_ops.OpError, match="unknown product operation"):
            product_invoke.product_command(name, expose_tier2=False)


def test_rest_cross_spelling_retry_replays_one_edit(vault, monkeypatch) -> None:
    from test_rest_registry import _auth, _client

    relative = "Knowledge Base/Notes/Insights/alias-retry.md"
    (vault / relative).write_text(
        "---\ntype: insight\nstatus: active\n---\n\nBefore\n", encoding="utf-8"
    )
    client = _client(vault, monkeypatch, EXOMEM_REST_API_KEY="sekret")
    arguments = {
        "path": relative,
        "why": "correct the same statement once",
        "operation": {"kind": "replace_string", "old_string": "Before", "new_string": "After"},
    }
    headers = {**_auth(), "Idempotency-Key": "same-public-edit"}
    first = client.post("/api/edit_memory", json=arguments, headers=headers)
    written = (vault / relative).stat().st_mtime_ns
    retry = client.post("/api/edit_note", json=arguments, headers=headers)
    assert first.status_code == retry.status_code == 200, (first.text, retry.text)
    assert first.json() == retry.json()
    assert (vault / relative).stat().st_mtime_ns == written
    assert (vault / relative).read_text(encoding="utf-8").count("After") == 1


@pytest.mark.parametrize("name", ("ask_memory", "search_memory", "ask-memory", "search-memory"))
def test_cli_search_spellings_return_the_same_evidence(vault, capsys, name) -> None:
    from exomem.__main__ import main

    assert main([name, "metabolism", "--mode", "keyword", "--json"]) == 0
    result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert result["success"] is True
    assert result["data"]


def test_cli_help_recommends_preferred_names_only(vault, capsys) -> None:
    from exomem.__main__ import _core_op_main

    with pytest.raises(SystemExit) as exit:
        _core_op_main(["--help"])
    assert exit.value.code == 0
    help_text = capsys.readouterr().out
    for command in commands.PRODUCT_COMMANDS:
        if command.preferred_name and "cli" in command.surfaces:
            assert command.public_name in help_text
            assert command.name not in help_text


@pytest.mark.parametrize("policy", ("cloud", "tier"))
def test_policy_withholds_both_product_spellings(vault, monkeypatch, policy) -> None:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(vault.parent / "excluded-lease"))
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1" if policy == "cloud" else "0")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", "name-proof")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN", "a" * 40)
    if policy == "cloud":
        monkeypatch.setattr(commands, "CLOUD_SURFACE_EXCLUSIONS", {"remember": commands.SurfaceExclusion("remember", "fixture exclusion", "fixture removed")})
    else:
        monkeypatch.setenv("EXOMEM_DISABLE_TIER2", "1")
        monkeypatch.setattr(commands, "PRODUCT_COMMANDS", tuple(replace(command, tier=2) if command.name == "remember" else command for command in commands.PRODUCT_COMMANDS))
    mcp = server.build_server(require_auth=False)

    async def exercise():
        for name in ("remember", "create_note"):
            assert await mcp.get_tool(name) is None

    try:
        asyncio.run(exercise())
    finally:
        writer_lease.reset_managers_for_tests()


def test_search_spellings_keep_the_same_restricted_disclosure(vault, monkeypatch) -> None:
    from test_restricted_refusal_floor import EXTERNAL, _restrict_external, _write_page

    from exomem import freshness, lexstore, memory_refs
    from exomem.governance import egress, principal

    relative = "Knowledge Base/Notes/Hidden/name-parity.md"
    _write_page(vault, relative, memory_refs.new_id())
    _restrict_external(vault, folder="Hidden", ceiling=egress.LEVEL_NONE)
    entries = [(str(path), freshness.stat_signature(path)) for path in vault.rglob("*.md")]
    freshness.seed(vault, "kb", entries)
    freshness.seed(vault, "vault", entries)
    lexstore.ensure_fresh(vault)
    monkeypatch.setattr(principal, "resolve_mcp_principal", lambda: replace(EXTERNAL, issuer_family="mcp"))
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(vault.parent / "restricted-lease"))
    mcp = server.build_server(require_auth=False)

    async def exercise():
        async with Client(mcp) as client:
            results = [
                (await client.call_tool(name, {"query": "Example", "mode": "keyword"})).data
                for name in ("ask_memory", "search_memory")
            ]
            assert results[0] == results[1]
            assert relative not in json.dumps(results)

    try:
        asyncio.run(exercise())
    finally:
        writer_lease.reset_managers_for_tests()


def test_standalone_hooks_keep_both_spellings_in_step_with_the_registry() -> None:
    from exomem._hooks import exomem_capture_nudge as capture
    from exomem._hooks import exomem_retrieve_nudge as retrieve

    activation = resolve_command("recall_context", commands.PRODUCT_COMMANDS)
    assert retrieve._ANCHOR_TOOL_SUFFIXES == (activation.name, activation.public_name)
    metadata = {"activation": {"operation": activation.name, "default": 4000, "minimum": 500, "maximum": 8000}}
    for name in activation.callable_names:
        assert retrieve._tool_refs(f"mcp__exomem__{name}", {"anchor": "visible-ref"}) == ["visible-ref"]
        assert retrieve._refresh_activation_cost(metadata, name, {"max_chars": 6000}) == 6000
    for name in resolve_command("edit_note", commands.PRODUCT_COMMANDS).callable_names:
        tool = {"name": f"mcp__exomem__{name}", "input": {"validate_only": True}}
        assert capture._successful_kb_write(tool) is False
        tool["input"]["validate_only"] = False
        assert capture._successful_kb_write(tool) is True
    preparation = resolve_command("prepare_note", commands.PRODUCT_COMMANDS)
    assert preparation.read_only is True
    assert capture._PREPARATION_TOOL_SUFFIXES == (preparation.name, preparation.public_name)
    for name in preparation.callable_names:
        assert capture._successful_kb_write({"name": f"mcp__exomem__{name}"}) is False
