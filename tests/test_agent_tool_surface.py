"""The agent tool surface through an in-process MCP client, one real workflow per test.

Each test lists or calls tools the way a host does and asserts what the vault holds afterwards,
not the wording of a reply. Nothing here calls a leaf directly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp import Client

from exomem import call_ledger, writer_lease
from exomem import server as server_module

LISTED = {
    "recall_context", "search_memory", "read_page", "read_instructions", "create_note", "add_observation",
    "edit_note", "supersede_note", "save_source", "save_evidence", "save_episode", "upload_files",
    "download_files", "query_records", "add_record", "edit_record", "query_plan_items", "add_plan_item",
    "edit_plan_item", "list_review_items", "decide_review_item", "inspect_memory", "manage_memory",
}


@pytest.fixture
def build(vault, monkeypatch, tmp_path):
    monkeypatch.setattr(server_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "1")
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease"))
    monkeypatch.setenv("EXOMEM_CALL_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.delenv("EXOMEM_DISABLE_TIER2", raising=False)
    call_ledger.reset_chain_cache()
    yield lambda: server_module.build_server(require_auth=False)
    writer_lease.reset_managers_for_tests()


def _session(mcp, scenario):
    async def run():
        async with Client(mcp) as client:
            return await scenario(client)

    return asyncio.run(run())


async def _call(client, name, arguments):
    result = await client.call_tool(name, arguments, raise_on_error=False)
    return result.structured_content if result.structured_content is not None else {
        "is_error": result.is_error,
        "text": " ".join(getattr(block, "text", "") for block in result.content),
    }


def test_a_local_server_lists_exactly_the_agent_tools(build) -> None:
    async def scenario(client):
        return [tool.name for tool in await client.list_tools()]

    names = _session(build(), scenario)

    assert set(names) == LISTED
    assert len(names) == len(LISTED)


def test_an_old_tool_name_is_refused_and_the_reply_lists_the_new_ones(build) -> None:
    async def scenario(client):
        return await _call(client, "ask_memory", {"query": "x"})

    reply = _session(build(), scenario)

    assert reply["is_error"] is True
    assert "search_memory" in reply["text"]


def _vault_text(vault: Path, needle: str) -> str:
    for path in (vault / "Knowledge Base").rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        if needle in text:
            return text
    return ""


def test_an_agent_reads_creates_and_edits_notes_by_title(build, vault) -> None:
    async def scenario(client):
        found = await _call(client, "search_memory", {"query": "retry fixed interval"})
        page = await _call(client, "read_page", {"page": "Retry with a fixed interval"})
        created = await _call(client, "create_note", {
            "title": "Retry policy", "type": "insight",
            "content": (
                "# Retry policy\n\n## Observations\n\n"
                "- [operating constraint] Cap retries at 30 s #reliability\n\n"
                "## Relations\n\n- refines [[Retry with a fixed interval]]\n"
            ),
        })
        edited = await _call(client, "edit_note", {
            "note": "Retry policy", "why": "Cap changed after review", "action": "replace_text",
            "old_text": "30 s", "new_text": "10 s",
        })
        return found, page, created, edited

    found, page, created, edited = _session(build(), scenario)

    assert "error" not in found and "error" not in page, (found, page)
    assert "error" not in created, created
    assert "error" not in edited, edited
    text = _vault_text(vault, "Retry policy")
    assert "Cap retries at 10 s" in text
    assert "30 s" not in text
