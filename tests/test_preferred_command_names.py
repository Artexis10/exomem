"""Public spellings share execution identity without duplicating discovery."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from _public_call import public_call
from fastmcp import Client

from exomem import (
    cli_ops,
    commands,
    product_invoke,
    public_surface,
    public_tools,
    server,
    writer_lease,
)
from exomem.command_surface import resolve_command

#: The tools a host lists on a local server; Cloud withholds the file transfers and lists its own two.
_LISTED = set(public_tools.TOOLS) | set(public_surface.DOORS.values()) | {"recall_context"}
_CLOUD_TRANSFERS = {"observe_transfer", "transfer_memory"}


def test_name_resolution_preserves_filtered_command_identity() -> None:
    for command in commands.PRODUCT_COMMANDS:
        assert resolve_command(command.public_name, (command,)) is command
        assert resolve_command(command.name, (command,)) is command
        assert resolve_command(command.public_name, ()) is None
        assert resolve_command(command.name, ()) is None
    # 34 MCP commands, two Cloud-only commands and one owner CLI/REST readout.
    assert len(commands.PRODUCT_COMMANDS) == len(set(commands.PRODUCT_PUBLIC_NAMES)) == 37
    assert len(commands.product_commands_for("mcp")) == 34
    assert any(command.name == "served_context" for command in commands.product_commands_for("rest"))
    assert all(command.name != "served_context" for command in commands.product_commands_for("mcp"))
    assert sum(command.public_name != command.name for command in commands.PRODUCT_COMMANDS) == 7


@pytest.mark.usefixtures("anchors_packet")  # tests the rollback packet; maps is the default
@pytest.mark.parametrize("cloud", (False, True))
def test_mcp_lists_the_public_tools_and_each_reaches_guarded_execution(vault, monkeypatch, cloud) -> None:
    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(vault.parent / "name-lease"))
    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1" if cloud else "0")
    if cloud:
        monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", "name-proof")
        monkeypatch.setenv("EXOMEM_CLOUD_CELL_TOKEN", "a" * 40)
    mcp = server.build_server(require_auth=False)

    async def exercise():
        async with Client(mcp) as client:
            discovered = {tool.name for tool in await client.list_tools()}
            if cloud:
                # The cell has no upload bridge here, so both file transfers stay withheld.
                assert discovered == (_LISTED - {"upload_files", "download_files"}) | _CLOUD_TRANSFERS
            else:
                assert discovered == _LISTED
            for command in commands.product_commands_for("mcp", cloud=cloud):
                # A grouped command stays registered for REST and the CLI, and no host lists it.
                assert await mcp.get_tool(command.name) is not None
                if command.name not in discovered:
                    refused = await client.call_tool(command.name, {}, raise_on_error=False)
                    assert refused.is_error
            result = await client.call_tool(*public_call("search_memory", {"query": "metabolism", "mode": "keyword"}))
            assert result.data
            result = await client.call_tool(*public_call("recall_context", {"turn": ""}))
            assert result.data["abstained"] is True
            result = await client.call_tool(*public_call("compile_source", {"sources": ["Knowledge Base/Sources/Books/2026-05-03-elimination-diet.md"]}))
            assert result.data
            for name in ("create_note", "supersede_note"):
                arguments = {
                    "title": "Public spelling proof",
                    "content": "## Observations\n\n- [constraint] Keep mutation receipts stable.\n",
                    "preview": True,
                }
                if name == "supersede_note":
                    arguments |= {
                        "note": "Knowledge Base/Notes/Insights/progressive-disclosure-without-mode-fragmentation.md",
                        "why": "prove the guard",
                    }
                result = await client.call_tool(name, arguments)
                assert result.data["mutated"] is False
                assert result.data["draft_id"]
            result = await client.call_tool(
                "edit_note",
                {"note": "missing.md", "why": "prove the guard", "action": "replace_text", "old_text": "old", "new_text": "new"},
                raise_on_error=False,
            )
            assert result.is_error or "error" in (result.structured_content or {})
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
    assert retry.json() == {**first.json(), "data": {**first.json()["data"], "replayed": True}}
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


def test_search_memory_keeps_the_restricted_disclosure(vault, monkeypatch) -> None:
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
            result = (await client.call_tool("search_memory", {"query": "Example", "options": {"mode": "keyword"}})).data
            assert "error" not in result
            assert relative not in json.dumps(result)

    try:
        asyncio.run(exercise())
    finally:
        writer_lease.reset_managers_for_tests()


def test_standalone_hooks_keep_the_public_names_in_step_with_the_registry() -> None:
    from exomem._hooks import exomem_capture_nudge as capture
    from exomem._hooks import exomem_retrieve_nudge as retrieve

    activation = resolve_command("recall_context", commands.PRODUCT_COMMANDS)
    assert retrieve._ANCHOR_TOOL_SUFFIXES == (activation.name, activation.public_name)
    metadata = {"activation": {"operation": activation.name, "default": 4000, "minimum": 500, "maximum": 8000}}
    for name in activation.callable_names:
        assert retrieve._tool_refs(f"mcp__exomem__{name}", {"anchor": "visible-ref"}) == ["visible-ref"]
        assert retrieve._refresh_activation_cost(metadata, name, {"max_chars": 6000}) == 6000
    tool = {"name": "mcp__exomem__edit_note", "input": {"preview": True}}
    assert capture._successful_kb_write(tool) is False
    tool["input"]["preview"] = False
    assert capture._successful_kb_write(tool) is True
    # A read door and a preparation that writes nothing never count as a saved write.
    assert capture._successful_kb_write({"name": "mcp__exomem__inspect_memory", "input": {"operation": "sources.compile"}}) is False
    assert capture._successful_kb_write({"name": "mcp__exomem__manage_memory", "input": {"operation": "files.create"}}) is True
